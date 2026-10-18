"""Command-line interface for cautious BLE discovery and protocol investigation."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable

from custom_components.surplife_ble.core import curtain_c2
from custom_components.surplife_ble.core.capture import JsonlCapture, load_replay, write_json
from custom_components.surplife_ble.core.client import LightClient
from custom_components.surplife_ble.core.framing import wrap_magichome_segments
from custom_components.surplife_ble.core.hci import (
    att_to_traffic_records,
    build_handle_to_uuid,
    extract_att_records,
    load_gatt_profile,
    parse_btsnoop,
    summarize_connections,
)
from custom_components.surplife_ble.core.scanner import discover_devices, find_device
from custom_components.surplife_ble.core.statediff import (
    analyze_states,
    collect_states_from_captures,
    diff_states,
    extract_state_from_capture,
    format_analysis,
    format_diff,
    format_recommendations,
    generate_recommendations,
)

CURTAIN_RESPONSE_TIMEOUT = 1.5
UPLOAD_ACK_TIMEOUT = 4.0


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def _max_frames(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > curtain_c2.MAX_UPLOAD_FRAMES:
        raise argparse.ArgumentTypeError(
            f"the device holds at most {curtain_c2.MAX_UPLOAD_FRAMES} frames "
            f"({curtain_c2.MAX_UPLOAD_BYTES} bytes at 20x20)"
        )
    return parsed


def _percentage(value: str) -> int:
    parsed = int(value)
    if not 0 <= parsed <= 100:
        raise argparse.ArgumentTypeError("value must be from 0 to 100")
    return parsed


def _rgb(value: str) -> tuple[int, int, int]:
    try:
        channels = tuple(int(item.strip()) for item in value.split(","))
    except ValueError as err:
        raise argparse.ArgumentTypeError("RGB must be formatted R,G,B") from err
    if len(channels) != 3 or any(channel < 0 or channel > 255 for channel in channels):
        raise argparse.ArgumentTypeError("RGB channels must each be from 0 to 255")
    return channels  # type: ignore[return-value]


def _hue_180(red: int, green: int, blue: int) -> int:
    """Convert RGB to hue on the 0-180 scale used by the IOTBT state frame."""
    import colorsys

    h, _s, _v = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
    return round(h * 180) & 0xFF


def _hex_bytes(value: str) -> bytes:
    compact = value.replace(" ", "").replace(":", "")
    try:
        payload = bytes.fromhex(compact)
    except ValueError as err:
        raise argparse.ArgumentTypeError("payload must be hexadecimal bytes") from err
    if not payload:
        raise argparse.ArgumentTypeError("payload cannot be empty")
    return payload


def _add_device(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--device", required=True, help="MAC address or macOS CoreBluetooth UUID")
    parser.add_argument(
        "--scan-timeout", type=_positive_float, default=10.0, help="seconds to find the device"
    )


def _add_control_options(parser: argparse.ArgumentParser) -> None:
    _add_device(parser)
    parser.add_argument("--jsonl", help="append a traffic transcript to this path")
    parser.add_argument(
        "--experimental-e122",
        action="store_true",
        help="explicitly test the unverified 20-segment E1 22 candidate on a matching model",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ble-rgb",
        description="Discover and cautiously probe Surplife/MagicHome2 BLE lights.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="actively scan without connecting")
    scan.add_argument("--duration", type=_positive_float, default=10.0)
    scan.add_argument("--json", help="write JSON advertisement snapshots")

    inspect_parser = subparsers.add_parser("inspect", help="connect and enumerate GATT")
    _add_device(inspect_parser)
    inspect_parser.add_argument("--json", help="write the GATT profile as JSON")

    probe = subparsers.add_parser("probe", help="subscribe and issue state-query candidates")
    _add_device(probe)
    probe.add_argument("--allow-probe-write", action="store_true", required=True)
    probe.add_argument("--jsonl", help="append a complete traffic transcript")
    probe.add_argument("--response-timeout", type=_positive_float, default=1.25)

    on = subparsers.add_parser("on", help="resolve the device, then turn it on")
    _add_control_options(on)
    off = subparsers.add_parser("off", help="resolve the device, then turn it off")
    _add_control_options(off)

    color = subparsers.add_parser("color", help="resolve the device, then set whole-lamp RGB")
    _add_control_options(color)
    color.add_argument("--rgb", required=True, type=_rgb)
    color.add_argument("--brightness", type=_percentage, default=100)

    white = subparsers.add_parser("white", help="resolve the device, then set single white")
    _add_control_options(white)
    white.add_argument("--brightness", required=True, type=_percentage)

    brightness = subparsers.add_parser("brightness", help="resolve and preserve the current mode")
    _add_control_options(brightness)
    brightness.add_argument("--value", required=True, type=_percentage)

    raw = subparsers.add_parser("raw-write", help="write reviewed bytes to one characteristic")
    _add_device(raw)
    raw.add_argument("--characteristic", required=True)
    raw.add_argument("--hex", dest="payload", required=True, type=_hex_bytes)
    raw.add_argument("--allow-raw-write", action="store_true", required=True)
    raw.add_argument("--jsonl", help="append a traffic transcript")

    replay = subparsers.add_parser("replay", help="replay reviewed TX records from JSONL")
    _add_device(replay)
    replay.add_argument("--capture", required=True)
    replay.add_argument("--allow-raw-write", action="store_true", required=True)
    replay.add_argument("--delay", type=float, default=0.05)
    replay.add_argument("--limit", type=int, help="replay only the first N TX records")
    replay.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be sent without connecting or writing",
    )
    replay.add_argument("--jsonl", help="append replay traffic to a different transcript")

    hci = subparsers.add_parser(
        "hci-convert",
        help="convert an Android btsnoop HCI log into reviewed JSONL traffic",
    )
    hci.add_argument("--snoop", required=True, help="path to btsnoop_hci.log")
    hci.add_argument("--device", required=True, help="device identifier for the JSONL output")
    hci.add_argument(
        "--gatt",
        help="GATT profile JSON from 'inspect --json' for handle-to-UUID mapping",
    )
    hci.add_argument(
        "--connection-handle",
        type=lambda value: int(value, 0),
        help="filter to one BLE connection handle; use --list-connections to see them",
    )
    hci.add_argument(
        "--list-connections",
        action="store_true",
        help="list connection handles found in the snoop log and exit",
    )
    hci.add_argument("--jsonl", help="write the converted traffic as JSONL")

    diff = subparsers.add_parser(
        "diff",
        help="compare two EA 81 state captures byte-by-byte to decode field layout",
    )
    diff.add_argument("--a", dest="hex_a", help="first state as hex")
    diff.add_argument("--b", dest="hex_b", help="second state as hex")
    diff.add_argument(
        "--a-capture", dest="capture_a", help="JSONL capture to extract first state from"
    )
    diff.add_argument(
        "--b-capture", dest="capture_b", help="JSONL capture to extract second state from"
    )
    diff.add_argument("--label-a", default="A", help="label for the first state in output")
    diff.add_argument("--label-b", default="B", help="label for the second state in output")

    watch = subparsers.add_parser(
        "watch",
        help="continuously probe and auto-log state changes as you change them in the app",
    )
    _add_device(watch)
    watch.add_argument(
        "--interval",
        type=_positive_float,
        default=2.0,
        help="seconds between state queries (default 2)",
    )
    watch.add_argument(
        "--jsonl",
        required=True,
        help="append state transitions to this JSONL file",
    )
    watch.add_argument(
        "--timeout",
        type=_positive_float,
        default=300.0,
        help="stop after this many seconds (default 300)",
    )

    analyze = subparsers.add_parser(
        "analyze",
        help="batch-analyze watch captures to produce a full field map",
    )
    analyze.add_argument(
        "--captures",
        nargs="+",
        required=True,
        help="JSONL files from 'watch' or 'probe' to analyze",
    )

    shell = subparsers.add_parser(
        "shell",
        help="interactive mode: connect once, issue multiple commands",
    )
    _add_device(shell)
    shell.add_argument("--jsonl", help="append a traffic transcript")

    _add_curtain_parsers(subparsers)
    return parser


def _hsv(value: str) -> tuple[int, int]:
    try:
        parts = tuple(int(item.strip()) for item in value.split(","))
    except ValueError as err:
        raise argparse.ArgumentTypeError("HSV must be formatted H,S") from err
    if len(parts) != 2 or not 0 <= parts[0] <= 359 or not 0 <= parts[1] <= 100:
        raise argparse.ArgumentTypeError("H must be 0-359 and S must be 0-100")
    return parts  # type: ignore[return-value]


def _solid_colors(value: str) -> list[tuple[int, int, int]]:
    return [_rgb(item) for item in value.split(";") if item.strip()]


def _byte(value: str) -> int:
    parsed = int(value, 0)
    if not 0 <= parsed <= 255:
        raise argparse.ArgumentTypeError("value must be from 0 to 255")
    return parsed


def _add_play_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--mode",
        type=_byte,
        default=1,
        help="play mode (default 1, what the Surplife app sends; 0 also loops all frames)",
    )
    parser.add_argument(
        "--speed",
        type=_percentage,
        default=50,
        help="0-100; 0-10 is about 5 s per frame, 50 about 0.5-1 s, 100 very fast (default 50)",
    )
    parser.add_argument("--brightness", type=_percentage, default=100, help="0-100 (default 100)")


def _add_curtain_options(parser: argparse.ArgumentParser) -> None:
    _add_device(parser)
    parser.add_argument(
        "--allow-curtain-c2",
        action="store_true",
        required=True,
        help="confirm that the target is an IOTBT curtain (product 0xC2) and writes are allowed",
    )
    parser.add_argument("--jsonl", help="append a traffic transcript to this path")
    parser.add_argument(
        "--response-timeout",
        type=_positive_float,
        default=CURTAIN_RESPONSE_TIMEOUT,
        help="seconds to wait for the EA 81 state notification (default 1.5)",
    )


def _add_curtain_parsers(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    curtain = subparsers.add_parser(
        "curtain",
        help="hardware-verified test commands for the IOTBT curtain (product 0xC2)",
        description=(
            "Send verified commands to an IOTBT437-style curtain light and print the "
            "decoded EA 81 state. Writes only to FF01 and never touches the OTA service. "
            "Home Assistant control for this codec stays locked."
        ),
    )
    sub = curtain.add_subparsers(dest="curtain_command", required=True)

    state = sub.add_parser("state", help="query and print the decoded EA 81 state")
    _add_curtain_options(state)
    on = sub.add_parser("on", help="turn the curtain on")
    _add_curtain_options(on)
    off = sub.add_parser("off", help="turn the curtain off")
    _add_curtain_options(off)

    color = sub.add_parser(
        "color",
        help="set a whole-curtain color (3B A1); brightness is resent with the color",
    )
    _add_curtain_options(color)
    group = color.add_mutually_exclusive_group(required=True)
    group.add_argument("--rgb", type=_rgb, help="R,G,B with channels 0-255")
    group.add_argument("--hsv", type=_hsv, help="H,S with hue 0-359 and saturation 0-100")
    color.add_argument("--brightness", type=_percentage, default=100, help="0-100 (default 100)")

    effect = sub.add_parser("effect", help="start a built-in effect (E0 02)")
    _add_curtain_options(effect)
    effect.add_argument(
        "--id",
        dest="effect_id",
        required=True,
        type=_byte,
        help="effect ID 0-255 (verified: 1 color cycle, 10 per-pixel multi-color pattern)",
    )
    effect.add_argument("--speed", type=_percentage, default=50, help="0-100 (default 50)")
    effect.add_argument("--brightness", type=_percentage, default=100, help="0-100 (default 100)")

    play = sub.add_parser(
        "play",
        help="play the uploaded animation (E2 09); the last play command survives power cycles",
    )
    _add_curtain_options(play)
    _add_play_options(play)

    upload = sub.add_parser(
        "upload",
        help="upload frames to device flash (EA 01..04), then play them",
        description=(
            "Upload one or more 20x20 frames to the curtain and play them. The upload is "
            "stored in flash and survives power cycles. Needs --allow-flash-write."
        ),
    )
    _add_curtain_options(upload)
    upload.add_argument(
        "--allow-flash-write",
        action="store_true",
        required=True,
        help="confirm that writing the animation to device flash is intended",
    )
    source = upload.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--solid",
        type=_solid_colors,
        help="R,G,B[;R,G,B...] one solid frame per color",
    )
    source.add_argument(
        "--image",
        help="PNG, JPEG, GIF or WebP file (all frames of animated images); needs Pillow",
    )
    upload.add_argument(
        "--resample",
        choices=("nearest", "box", "lanczos"),
        default="box",
        help="filter for resizing to 20x20 (default box)",
    )
    upload.add_argument(
        "--max-frames",
        type=_max_frames,
        default=curtain_c2.MAX_UPLOAD_FRAMES,
        help=(
            f"keep at most this many frames, with a warning (default and limit "
            f"{curtain_c2.MAX_UPLOAD_FRAMES}: the device rejects more than "
            f"{curtain_c2.MAX_UPLOAD_BYTES} bytes)"
        ),
    )
    upload.add_argument(
        "--controller-side",
        choices=("left", "right"),
        default="left",
        help=(
            "side of the image where the controller box hangs (default left): image column "
            "x=0 maps to the controller-side string; with right, x -> 19-x"
        ),
    )
    upload.add_argument(
        "--mirror",
        action="store_true",
        help="alias that flips the --controller-side choice (left <-> right)",
    )
    upload.add_argument("--flip", action="store_true", help="swap top and bottom (y -> 19 - y)")
    upload.add_argument(
        "--no-play", action="store_true", help="upload only; do not send the play command"
    )
    upload.add_argument(
        "--ack-timeout",
        type=_positive_float,
        default=UPLOAD_ACK_TIMEOUT,
        help="seconds to wait for each acked step before aborting (default 4)",
    )
    _add_play_options(upload)


async def _device_client(args: argparse.Namespace) -> LightClient:
    device, advertisement = await find_device(args.device, scan_timeout=args.scan_timeout)
    return LightClient(device, advertisement, capture_path=getattr(args, "jsonl", None))


async def _scan(args: argparse.Namespace) -> int:
    rows = await discover_devices(args.duration)
    snapshots = [snapshot.to_dict() for _device, _advertisement, snapshot in rows]
    if args.json:
        write_json(args.json, snapshots)
    for snapshot in snapshots:
        name = snapshot["local_name"] or snapshot["name"] or "(unknown)"
        services = ",".join(snapshot["service_uuids"]) or "-"
        print(f"{snapshot['identifier']:36} RSSI {snapshot['rssi']:4} {name} services={services}")
    print(f"Found {len(snapshots)} BLE devices")
    return 0


async def _inspect(args: argparse.Namespace) -> int:
    client = await _device_client(args)
    async with client:
        profile = await client.inspect_gatt()
    data = profile.to_dict()
    if args.json:
        write_json(args.json, data)
    print(json.dumps(data, indent=2))
    return 0


async def _probe(args: argparse.Namespace) -> int:
    client = await _device_client(args)
    async with client:
        resolution = await client.probe(
            allow_writes=args.allow_probe_write,
            response_timeout=args.response_timeout,
        )
        result = {
            "resolution": resolution.to_dict() if resolution else None,
            "state": client.state.to_dict() if client.state else None,
        }
    print(json.dumps(result, indent=2))
    return 0 if resolution else 2


async def _resolve_control(client: LightClient) -> None:
    resolution = await client.probe(allow_writes=True)
    if resolution is None:
        raise RuntimeError("no supported state response was received")
    if not resolution.repeatable:
        raise RuntimeError("state response was not repeatable; control remains locked")


async def _control(args: argparse.Namespace) -> int:
    client = await _device_client(args)
    async with client:
        await _resolve_control(client)
        if args.command == "on":
            await client.set_power(True)
        elif args.command == "off":
            await client.set_power(False)
        elif args.command == "color":
            await client.set_rgb(
                *args.rgb,
                brightness=max(1, args.brightness),
                experimental_e122=args.experimental_e122,
            )
        elif args.command == "white":
            await client.set_white(
                max(1, args.brightness), experimental_e122=args.experimental_e122
            )
        elif args.command == "brightness":
            await client.set_brightness(max(1, args.value))
        print(json.dumps(client.state.to_dict() if client.state else {}, indent=2))
    return 0


def _curtain_command(args: argparse.Namespace) -> curtain_c2.CurtainCommand | None:
    """Build the inner command for a `curtain` subcommand (None for `state`)."""
    name = args.curtain_command
    if name == "state":
        return None
    if name == "on":
        return curtain_c2.power(True)
    if name == "off":
        return curtain_c2.power(False)
    if name == "color":
        if args.hsv is not None:
            return curtain_c2.color_hsv(*args.hsv, args.brightness)
        return curtain_c2.color_rgb(*args.rgb, args.brightness)
    if name == "effect":
        return curtain_c2.effect(args.effect_id, args.speed, args.brightness)
    if name == "play":
        return curtain_c2.play(args.mode, args.speed, args.brightness)
    raise ValueError(f"unknown curtain command: {name}")


async def _curtain_send(client: LightClient, command: curtain_c2.CurtainCommand, note: str) -> None:
    frames = wrap_magichome_segments(
        command.payload,
        client.next_sequence(),
        command_family=command.cmd_id,
    )
    await client.send_frames(frames, note=note)


async def _curtain_wait_state(
    client: LightClient, wait_seconds: float
) -> curtain_c2.CurtainState | None:
    """Wait up to ``wait_seconds`` for an EA 81 state notification."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_seconds
    while (remaining := deadline - loop.time()) > 0:
        packet = await client.wait_notification(remaining)
        if packet is None:
            break
        state = curtain_c2.parse_state(packet)
        if state is not None:
            return state
    return None


async def _curtain_prepare(client: LightClient) -> None:
    """Enumerate GATT, acquire the MTU, and subscribe to FF02 before any write."""
    await client.inspect_gatt()
    mtu = await client.acquire_mtu()
    if mtu is not None:
        print(f"ATT MTU: {mtu}", file=sys.stderr)
    await client.subscribe_ff02()
    client.flush_notifications()


async def _curtain_state_or_query(
    client: LightClient, state: curtain_c2.CurtainState | None, wait_seconds: float
) -> curtain_c2.CurtainState | None:
    """Return ``state`` or, if it is None, query the device and wait for the reply."""
    if state is None:
        client.flush_notifications()
        await _curtain_send(client, curtain_c2.state_query(), "curtain state query")
        state = await _curtain_wait_state(client, wait_seconds)
    return state


def _curtain_print_state(state: curtain_c2.CurtainState | None) -> int:
    if state is None:
        print("no EA 81 state received", file=sys.stderr)
        return 2
    print(json.dumps(state.to_dict(), indent=2))
    print(
        "note: color and brightness are not reported by the device; only power is reliable.",
        file=sys.stderr,
    )
    return 0


async def _curtain(args: argparse.Namespace) -> int:
    """Run one verified curtain command and print the decoded EA 81 state."""
    if args.curtain_command == "upload":
        return await _curtain_upload(args)
    command = _curtain_command(args)
    client = await _device_client(args)
    async with client:
        await _curtain_prepare(client)
        state = None
        if command is not None:
            await _curtain_send(client, command, f"curtain {args.curtain_command}")
            state = await _curtain_wait_state(client, args.response_timeout)
        state = await _curtain_state_or_query(client, state, args.response_timeout)
    return _curtain_print_state(state)


def _upload_frames(
    args: argparse.Namespace,
) -> tuple[list[list[curtain_c2.RGB]], str]:
    """Build the buffer-order frames for `curtain upload` and a short source label."""
    if args.solid is not None:
        return [curtain_c2.solid_frame(color) for color in args.solid], "solid colors"
    loaded = curtain_c2.load_image_frames(
        args.image,
        resample=args.resample,
        max_frames=args.max_frames,
        controller_side=args.controller_side,
        mirror=args.mirror,
        flip=args.flip,
    )
    if loaded.total > len(loaded.frames):
        print(
            f"warning: {args.image} has {loaded.total} frames; keeping the first "
            f"{len(loaded.frames)} (--max-frames)",
            file=sys.stderr,
        )
    side = "right" if (args.controller_side == "right") != args.mirror else "left"
    print(f"controller side: {side} (image column {'19' if side == 'right' else '0'} "
          "is the controller-side string)")
    return loaded.frames, args.image


async def _curtain_upload(args: argparse.Namespace) -> int:
    """Upload frames to device flash, then optionally play them."""
    frames, source = _upload_frames(args)
    steps = curtain_c2.build_upload(frames)
    print(f"Uploading {len(frames)} frame(s) from {source} to device flash")
    client = await _device_client(args)
    async with client:
        await _curtain_prepare(client)
        await client.upload_steps(steps, ack_timeout=args.ack_timeout, progress=print)
        state = None
        if not args.no_play:
            command = curtain_c2.play(args.mode, args.speed, args.brightness)
            print(f"play: mode {args.mode} speed {args.speed} brightness {args.brightness}")
            await _curtain_send(client, command, "curtain upload play")
            state = await _curtain_wait_state(client, args.response_timeout)
        state = await _curtain_state_or_query(client, state, args.response_timeout)
    return _curtain_print_state(state)


async def _raw_write(args: argparse.Namespace) -> int:
    client = await _device_client(args)
    async with client:
        await client.raw_write(
            args.characteristic,
            args.payload,
            allow_raw_write=args.allow_raw_write,
        )
    print(f"Wrote {len(args.payload)} reviewed bytes to {args.characteristic}")
    return 0


async def _replay(args: argparse.Namespace) -> int:
    records = load_replay(args.capture, args.device)
    if not records:
        raise ValueError("capture contains no TX records for the selected device")
    if getattr(args, "limit", None):
        records = records[: args.limit]
    if args.dry_run:
        print(f"Dry run: {len(records)} TX records would be sent to {args.device}")
        for index, record in enumerate(records, 1):
            print(
                f"  {index:3d}. {record.characteristic}  "
                f"{record.payload_hex}  ({record.note or 'no note'})"
            )
        return 0
    client = await _device_client(args)
    async with client:
        await client.replay(records, allow_raw_write=args.allow_raw_write, delay=args.delay)
    print(f"Replayed {len(records)} reviewed TX records")
    return 0


async def _hci_convert(args: argparse.Namespace) -> int:
    raw_records = list(parse_btsnoop(args.snoop))
    att_records = list(extract_att_records(raw_records))

    if args.list_connections:
        summaries = summarize_connections(att_records)
        if not summaries:
            print("No ATT traffic found in the snoop log.")
            return 1
        print(f"Found {len(summaries)} connection(s) with ATT traffic:")
        for summary in summaries:
            handles = ", ".join(f"0x{handle:04x}" for handle in sorted(summary.attribute_handles))
            print(
                f"  handle 0x{summary.connection_handle:04x}: "
                f"{summary.write_count} writes, {summary.notification_count} notifications, "
                f"attributes=[{handles}]"
            )
        return 0

    handle_map = None
    if args.gatt:
        profile = load_gatt_profile(args.gatt)
        handle_map = build_handle_to_uuid(profile)

    traffic = att_to_traffic_records(
        att_records,
        device_id=args.device,
        handle_map=handle_map,
        connection_handle=args.connection_handle,
    )
    if not traffic:
        print("No ATT write or notification records matched the filter.")
        return 1

    if args.jsonl:
        capture = JsonlCapture(args.jsonl)
        for record in traffic:
            capture.write(record)

    tx_count = sum(1 for r in traffic if r.direction == "tx")
    rx_count = sum(1 for r in traffic if r.direction == "rx")
    print(f"Converted {len(traffic)} ATT records ({tx_count} TX, {rx_count} RX)")
    for index, record in enumerate(traffic, 1):
        print(
            f"  {index:3d}. {record.direction.upper()}  {record.characteristic}  "
            f"{record.payload_hex}  ({record.note or 'no note'})"
        )
    if not args.jsonl:
        print("\nPass --jsonl PATH to write these records for replay.")
    return 0


async def _diff(args: argparse.Namespace) -> int:
    state_a: bytes | None = None
    state_b: bytes | None = None
    if args.hex_a:
        state_a = bytes.fromhex(args.hex_a.replace(" ", "").replace(":", ""))
    elif args.capture_a:
        state_a = extract_state_from_capture(args.capture_a)
        if state_a is None:
            print(f"error: no EA 81 state found in {args.capture_a}", file=sys.stderr)
            return 1
    if args.hex_b:
        state_b = bytes.fromhex(args.hex_b.replace(" ", "").replace(":", ""))
    elif args.capture_b:
        state_b = extract_state_from_capture(args.capture_b)
        if state_b is None:
            print(f"error: no EA 81 state found in {args.capture_b}", file=sys.stderr)
            return 1
    if state_a is None or state_b is None:
        print(
            "error: provide both --a and --b (hex), or --a-capture and --b-capture (JSONL)",
            file=sys.stderr,
        )
        return 1
    result = diff_states(state_a, state_b)
    print(format_diff(result, label_a=args.label_a, label_b=args.label_b))
    return 0


async def _watch_probe_once(device_id: str, scan_timeout: float) -> bytes | None:
    """Connect, probe, disconnect. Returns None if the device can't be reached."""
    try:
        device, advertisement = await find_device(device_id, scan_timeout=scan_timeout)
    except (LookupError, Exception):
        return None
    client = LightClient(device, advertisement, capture_path=None)
    try:
        async with client:
            return await client.query_raw_state()
    except Exception:
        return None


def _watch_print_live_analysis(all_states: list[bytes]) -> None:
    """Print the current field map and recommendations from all captured states."""
    if not all_states:
        return
    from custom_components.surplife_ble.core.statediff import (
        analyze_states,
        generate_recommendations,
    )

    analysis = analyze_states(all_states)
    recs = generate_recommendations(analysis)
    high_count = sum(1 for r in recs if r.priority == "high")
    dynamic = analysis.dynamic_offsets
    print(f"  --- live analysis: {len(all_states)} states, "
          f"{len(dynamic)} dynamic positions: {dynamic}")
    if high_count == 0:
        print("  --- all high-priority fields confirmed! Control may be ready to unlock.")
    else:
        print(f"  --- {high_count} high-priority capture(s) remaining:")
        for rec in recs:
            if rec.priority != "high":
                continue
            print(f"      {rec.app_mode}: {rec.action}")
    print()


def _watch_log_transition(
    capture: JsonlCapture,
    device_id: str,
    transition_count: int,
    previous: bytes,
    current: bytes,
) -> list[int]:
    """Log a state transition to the capture file and return changed offsets."""
    from datetime import UTC, datetime

    changed_offsets = [
        i for i, (a, b) in enumerate(zip(previous, current, strict=False)) if a != b
    ]
    now = datetime.now(UTC).isoformat()
    capture.write(
        {
            "timestamp": now,
            "device_id": device_id,
            "direction": "transition",
            "state_hex": current.hex(),
            "previous_hex": previous.hex(),
            "changed_offsets": changed_offsets,
            "note": f"transition {transition_count}",
        }
    )
    print(f"\n  [{transition_count}] {now}  changed pos {changed_offsets}:")
    print(f"    was: {previous.hex()}")
    print(f"    now: {current.hex()}")
    return changed_offsets


async def _watch(args: argparse.Namespace) -> int:
    """Continuously probe and auto-log state changes with live analysis.

    Uses connect-probe-disconnect cycles so the lamp's single BLE connection slot
    is freed between probes, allowing the Surplife app to connect and change state.
    After each state change, automatically runs analysis and prints the current
    field map plus recommendations — no need to restart.
    """
    from datetime import UTC, datetime

    capture = JsonlCapture(args.jsonl)
    all_states: list[bytes] = []
    # watch uses a short scan timeout by default since we're looping anyway
    scan_timeout = args.scan_timeout if args.scan_timeout != 10.0 else 1.0
    print(f"Watching {args.device} for state changes (every {args.interval}s)...")
    print(f"  scan timeout: {scan_timeout}s per probe")
    print("Change states in the Surplife app between probes. Press Ctrl+C to stop.")
    print("  (the Mac disconnects between probes so the app can connect)")
    print("  (if the app is connected, the Mac will wait and retry)")
    print("  (after each change, live analysis + recommendations are printed)")
    print()

    # Get the initial state, waiting for the app to disconnect if needed.
    print("  Waiting for device (close the Surplife app if it's connected)...")
    previous: bytes | None = None
    while previous is None:
        previous = await _watch_probe_once(args.device, scan_timeout)
        if previous is None:
            print("  ... device not reachable (app may be connected), retrying...")
            await asyncio.sleep(args.interval)

    print(f"  Initial state: {previous.hex()}")
    all_states.append(previous)
    capture.write(
        {
            "timestamp": datetime.now(UTC).isoformat(),
            "device_id": args.device,
            "direction": "state",
            "state_hex": previous.hex(),
            "note": "initial state",
        }
    )
    _watch_print_live_analysis(all_states)

    start = asyncio.get_running_loop().time()
    transition_count = 0
    miss_count = 0

    while True:
        elapsed = asyncio.get_running_loop().time() - start
        if elapsed >= args.timeout:
            print(f"\nTimeout reached ({args.timeout}s). Stopping.")
            break

        await asyncio.sleep(args.interval)
        current = await _watch_probe_once(args.device, scan_timeout)

        if current is None:
            miss_count += 1
            if miss_count == 1:
                print("  (device not reachable — app may be connected, waiting...)")
            continue

        if miss_count > 0:
            print(f"  (device reachable again after {miss_count} missed probe(s))")
            miss_count = 0

        if current == previous:
            continue

        transition_count += 1
        _watch_log_transition(
            capture, args.device, transition_count, previous, current
        )
        all_states.append(current)
        previous = current
        _watch_print_live_analysis(all_states)

    print(f"\nRecorded {transition_count} state transition(s) to {args.jsonl}")
    print(f"Captured {len(all_states)} total states.")
    return 0


async def _analyze(args: argparse.Namespace) -> int:
    """Batch-analyze multiple capture files to produce a full field map."""
    states = collect_states_from_captures(args.captures)
    if not states:
        print("No EA 81 states found in the provided capture files.", file=sys.stderr)
        return 1
    analysis = analyze_states(states)
    print(format_analysis(analysis))
    print()
    recs = generate_recommendations(analysis)
    print(format_recommendations(recs))
    return 0


async def _print_state_after_command(client: LightClient, delay: float = 0.2) -> None:
    """Wait briefly then query and print state after a control command."""
    await asyncio.sleep(delay)
    state = await client.query_state()
    if state:
        print(json.dumps(state.to_dict(), indent=2))
    else:
        print(json.dumps(client.state.to_dict() if client.state else {}, indent=2))


async def _shell(args: argparse.Namespace) -> int:
    """Interactive mode: connect once, issue multiple commands without reconnecting."""
    import shlex

    client = await _device_client(args)
    print(f"Connecting to {args.device}...")
    async with client:
        await _resolve_control(client)
        print(f"Connected. Codec: {client.resolution.codec if client.resolution else 'unknown'}")
        if client.state:
            print(f"Current state: {client.state.to_dict()}")
        print()
        print("Commands: on, off, color R,G,B [BRIGHTNESS], white BRIGHTNESS, "
              "brightness VALUE, state, try-color R,G,B [BRIGHT], raw HEX, help, quit")
        print()

        while True:
            try:
                line = await asyncio.to_thread(input, "ble-rgb> ")
            except (EOFError, KeyboardInterrupt):
                print()
                break

            line = line.strip()
            if not line:
                continue

            parts = shlex.split(line)
            cmd = parts[0].lower()

            try:
                if cmd in ("quit", "exit", "q"):
                    break
                elif cmd == "help":
                    print("  on                    - turn lamp on")
                    print("  off                   - turn lamp off")
                    print(
                        "  color R,G,B [BRIGHT]  - set RGB color (0-255), "
                        "optional brightness 0-100"
                    )
                    print("  white BRIGHTNESS      - set white mode at brightness 0-100")
                    print("  brightness VALUE      - set brightness 0-100, keep current color/mode")
                    print("  state                 - query current state from the lamp")
                    print(
                        "  try-color R,G,B [BRT] - send all known color command formats"
                    )
                    print("  raw HEX               - send raw hex bytes (no framing)")
                    print("  quit                  - disconnect and exit")
                elif cmd == "on":
                    await client.set_power(True)
                    await _print_state_after_command(client)
                elif cmd == "off":
                    await client.set_power(False)
                    await _print_state_after_command(client)
                elif cmd == "color":
                    if len(parts) < 2:
                        print("  usage: color R,G,B [BRIGHTNESS]")
                        continue
                    rgb = _rgb(parts[1])
                    brightness = 100
                    if len(parts) >= 3:
                        brightness = _percentage(parts[2])
                    await client.set_rgb(*rgb, brightness=max(1, brightness))
                    await _print_state_after_command(client)
                elif cmd == "white":
                    if len(parts) < 2:
                        print("  usage: white BRIGHTNESS")
                        continue
                    brightness = _percentage(parts[1])
                    await client.set_white(max(1, brightness))
                    await _print_state_after_command(client)
                elif cmd == "brightness":
                    if len(parts) < 2:
                        print("  usage: brightness VALUE")
                        continue
                    value = _percentage(parts[1])
                    await client.set_brightness(max(1, value))
                    await _print_state_after_command(client)
                elif cmd == "state":
                    state = await client.query_state()
                    if state:
                        print(json.dumps(state.to_dict(), indent=2))
                    else:
                        print("  no state response")
                elif cmd == "raw":
                    if len(parts) < 2:
                        print("  usage: raw HEX (e.g. raw e00100c100643200000000140000)")
                        continue
                    try:
                        payload = bytes.fromhex(parts[1].replace(" ", ""))
                    except ValueError:
                        print("  error: invalid hex string")
                        continue
                    if not 1 <= len(payload) <= 512:
                        print(f"  error: payload must be 1-512 bytes (got {len(payload)})")
                        continue
                    from custom_components.surplife_ble.core.protocol import (
                        frame_for_transport,
                    )
                    transport = (
                        client._probe_frame.transport
                        if client._probe_frame
                        else "magichome2"
                    )
                    framed = frame_for_transport(
                        payload, transport, client._sequence.next()
                    )
                    await client._write(framed, framing=transport, note="shell raw")
                    print(f"  sent {len(payload)} bytes: {payload.hex()}")
                    await asyncio.sleep(0.5)
                    state = await client.query_state()
                    if state:
                        print(json.dumps(state.to_dict(), indent=2))
                elif cmd == "try-color":
                    if len(parts) < 2:
                        print("  usage: try-color R,G,B [BRIGHTNESS]")
                        print("  sends all known color command formats and")
                        print("  queries state after each to see which worked.")
                        continue
                    # Accept "R,G,B BRIGHT" or "R,G,B,BRIGHT"
                    raw_parts = parts[1].split(",")
                    if len(raw_parts) == 4:
                        rgb = _rgb(",".join(raw_parts[:3]))
                        brightness = _percentage(raw_parts[3])
                    else:
                        rgb = _rgb(parts[1])
                        brightness = 100
                        if len(parts) >= 3:
                            brightness = _percentage(parts[2])
                    transport = (
                        client._probe_frame.transport
                        if client._probe_frame
                        else "magichome2"
                    )
                    from custom_components.surplife_ble.core.protocol import (
                        frame_for_transport,
                        iotbt_3b_rgb,
                        iotbt_e0_3b_rgb,
                        iotbt_e2_rgb,
                    )
                    candidates = [
                        ("E0+C1 (current)", bytes((
                            0xE0, 0x01, 0x00, 0xC1,
                            _hue_180(*rgb),
                            100, brightness, 0, 0, 0, 0, 0x14, 0, 0,
                        ))),
                        ("E0+3B+A1 (v3 unified)", iotbt_e0_3b_rgb(*rgb, brightness)),
                        ("3B+A1 (v2 standalone)", iotbt_3b_rgb(*rgb, brightness)),
                        ("E2+0B (IOTBT Telink)", iotbt_e2_rgb(*rgb, brightness)),
                    ]
                    for name, payload in candidates:
                        print(f"  trying {name}: {payload.hex()}")
                        framed = frame_for_transport(
                            payload, transport, client._sequence.next()
                        )
                        await client._write(framed, framing=transport, note=f"try-color {name}")
                        await asyncio.sleep(1.0)
                        state = await client.query_state()
                        if state:
                            print(f"    state: {state.raw_hex}")
                            print(f"    rgb={state.rgb} bright={state.brightness}")
                        else:
                            print("    no state response")
                        print()
                else:
                    print(f"  unknown command: {cmd}. Type 'help' for available commands.")
            except Exception as err:
                print(f"  error: {err}", file=sys.stderr)

    print("Disconnected.")
    return 0


async def async_main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers: dict[str, Callable[[argparse.Namespace], Awaitable[int]]] = {
        "scan": _scan,
        "inspect": _inspect,
        "probe": _probe,
        "on": _control,
        "off": _control,
        "color": _control,
        "white": _control,
        "brightness": _control,
        "raw-write": _raw_write,
        "replay": _replay,
        "hci-convert": _hci_convert,
        "diff": _diff,
        "watch": _watch,
        "analyze": _analyze,
        "shell": _shell,
        "curtain": _curtain,
    }
    return await handlers[args.command](args)


def main() -> int:
    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        return 130
    except Exception as err:
        print(f"error: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
