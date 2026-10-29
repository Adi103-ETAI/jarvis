"""Channels — OpenClaw-adapted messaging gateway rim (phase 8).

A channel is a trust SOURCE, never an authority (D5/D7): policy maps it
to a lane, admission is policy mechanics, and the console spine (Gate
A/B) keeps the only path to the kernel. See docs/phase8-gateway.md.
"""

from jarvis.channels.gateway import (ChannelConfig, ChannelGateway,
                                     PairingStore, load_channel_configs)
from jarvis.channels.telegram import AdapterError, TelegramAdapter

__all__ = ["ChannelConfig", "ChannelGateway", "PairingStore",
           "load_channel_configs", "AdapterError", "TelegramAdapter"]
