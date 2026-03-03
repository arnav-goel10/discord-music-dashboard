import logging

import wavelink


class DAVECompatiblePlayer(wavelink.Player):
    """Wavelink player variant that includes channelId in Lavalink voice updates.

    Discord's DAVE rollout requires Lavalink clients to pass the voice channel
    identifier in the voice state payload. Wavelink 3.4.1 does not include it.
    """

    async def _dispatch_voice_update(self) -> None:  # pragma: no cover - runtime integration path
        assert self.guild is not None
        data = self._voice_state["voice"]

        session_id = data.get("session_id")
        token = data.get("token")
        endpoint = data.get("endpoint")

        if not session_id or not token or not endpoint:
            return

        voice_payload = {
            "sessionId": session_id,
            "token": token,
            "endpoint": endpoint,
        }

        channel_id = getattr(getattr(self, "channel", None), "id", None)
        if channel_id is not None:
            voice_payload["channelId"] = str(channel_id)

        request = {"voice": voice_payload}

        try:
            await self.node._update_player(self.guild.id, data=request)
        except wavelink.LavalinkException:
            await self.disconnect()
        else:
            self._connection_event.set()

        logging.getLogger(__name__).debug("Player %s dispatched VOICE_UPDATE with channelId.", self.guild.id)
