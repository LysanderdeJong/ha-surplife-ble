"""Constants for the Surplife BLE integration."""

from datetime import timedelta

DOMAIN = "surplife_ble"
# The media_player, image and number platforms add entities only for the IOTBT curtain.
PLATFORMS = ["light", "media_player", "image", "number"]
UPDATE_INTERVAL = timedelta(seconds=15)
