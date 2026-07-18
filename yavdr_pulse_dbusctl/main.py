#!/usr/bin/env python3
import asyncio
import contextlib
import logging
import sdbus
import pulsectl

from typing import Any, NamedTuple

from yavdr_pulse_dbusctl import alsa


INTERFACE_NAME = "org.yavdr.PulseDBusCtl"
PULSE_OBJECT_PATH = "/org/yavdr/PulseDBusCtl"


class ProfileSwitchTimeoutError(sdbus.DbusFailedError):
    dbus_error_name = f"{INTERFACE_NAME}.Error.ProfileSwitchTimeout"


class DeviceNotFoundError(sdbus.DbusFailedError):
    dbus_error_name = f"{INTERFACE_NAME}.Error.DeviceNotFound"


class Sink(NamedTuple):
    name: str
    description: str
    idx: int
    card: int
    card_name: str
    is_muted: bool
    channel_count: int
    volume_values: list[float]
    port_active: str
    is_default_sink: bool


class OutputProfile(NamedTuple):
    name: str
    description: str
    profiles: list[tuple[str, str]]
    is_active: str


def wait_for_new_sink(card_idx, timeout=2.5):
    with pulsectl.Pulse("profile-watcher") as pulse_ev:
        # check if sink is already available
        sinks = [s for s in pulse_ev.sink_list() if s.card == card_idx]
        if sinks:
            return sinks[0]

        # wait for events
        start_time = time.time()
        while time.time() - start_time < timeout:
            # wait for the next event or return if the timeout is reached
            pulse_ev.event_mask_set("sink")

            # just stop the loop in the callback
            def stop_loop_cb(ev):
                raise pulsectl.PulseLoopStop

            pulse_ev.event_callback_set(stop_loop_cb)

            # calculate remaining time
            remaining = max(0.1, timeout - (time.time() - start_time))
            pulse_ev.event_listen(timeout=remaining)

            # check the sink list
            sinks = [s for s in pulse_ev.sink_list() if s.card == card_idx]
            if sinks:
                return sinks[0]

        return None


class PulseDBusControl(sdbus.DbusInterfaceCommonAsync, interface_name=INTERFACE_NAME):
    def __init__(self, pulse: pulsectl.Pulse | None) -> None:
        self.pulse: pulsectl.Pulse | None = pulse
        super().__init__()

    def update_pulse_instance(self, new_pulse_instance: pulsectl.Pulse):
        """Methode zur Laufzeit-Aktualisierung der pulsectl-Referenz"""
        self.pulse = new_pulse_instance

    @sdbus.dbus_method_async(
        result_signature="a(ssa(ss)s)",
        flags=sdbus.DbusUnprivilegedFlag,
    )
    async def list_output_profiles(self) -> list[OutputProfile]:
        if not self.pulse:
            raise sdbus.DBusFailedError(
                "PipeWire/Pulseaudio is currently not available."
            )
        try:
            cards: list[Any] = self.pulse.card_list()
            result: list[OutputProfile] = []
            for card in cards:
                profiles = []
                for p in card.profile_list:
                    if (
                        p.available
                        and p.available != "no"
                        and p.name != "off"
                        and p.n_sinks > 0
                        and (not p.name.startswith("input:"))
                    ):
                        profiles.append((p.name, p.description))
                    else:
                        logging.debug(f"ignoring {p=}")
                if profiles:
                    result.append(
                        OutputProfile(
                            card.name,
                            card.proplist["device.description"],
                            profiles,
                            card.profile_active.name,
                        )
                    )
            return result

        except (pulsectl.PulseError, AttributeError):
            raise sdbus.DBusFailedError("Error communicating with the audio service.")

    @sdbus.dbus_method_async(
        input_signature="ss", result_signature="b", flags=sdbus.DbusUnprivilegedFlag
    )
    async def set_profile(self, card_name: str, profile_name: str):
        if not self.pulse:
            raise sdbus.DBusFailedError(
                "PipeWire/Pulseaudio is currently not available."
            )
        try:
            try:
                card = self.pulse.get_card_by_name(card_name)
            except Exception as e:
                raise DeviceNotFoundError(f"Card '{card_name}' not found: {e}")

            profile = next(
                (p for p in card.profile_list if p.name == profile_name), None
            )  # type: ignore
            if not profile:
                raise DeviceNotFoundError(f"Profile '{profile_name}' not available")

            self.pulse.card_profile_set(card, profile)

            loop = asyncio.get_running_loop()
            new_sink = await loop.run_in_executor(
                None, lambda: wait_for_new_sink(card.index)
            )

            if not new_sink:
                raise ProfileSwitchTimeoutError(
                    f"Profile '{profile_name}' activated, but no audio sink appeared within 2.5 seconds"
                )

            logging.info(f"found {new_sink=}")
            self.pulse.default_set(new_sink)

            # Streams verschieben
            for stream in self.pulse.sink_input_list():
                self.pulse.sink_input_move(stream.index, new_sink.index)
            return True

        except (pulsectl.PulseError, AttributeError):
            raise sdbus.DBusFailedError("Error communicating with the audio service.")

    @sdbus.dbus_method_async(
        result_signature="a(ssixsbiadsb)s",
        flags=sdbus.DbusUnprivilegedFlag,
    )
    async def list_sinks(self) -> tuple[list[Sink], str]:
        if not self.pulse:
            raise sdbus.DBusFailedError(
                "PipeWire/Pulseaudio is currently not available."
            )
        try:
            pulse = self.pulse
            default_sink_name = pulse.server_info().default_sink_name
            cards = {c.index: c.name for c in pulse.card_list()}
            result = []

            for s in pulse.sink_list():
                result.append(
                    Sink(
                        s.name,
                        s.description,
                        s.index,
                        s.card,
                        cards.get(s.card, ""),
                        bool(s.mute),
                        s.channel_count,
                        list(s.volume.values),
                        s.port_active.available_state._value
                        if s.port_active
                        else "unknown",
                        s.name == default_sink_name,
                    )
                )

            logging.info(result)
            return (result, default_sink_name)
        except (pulsectl.PulseError, AttributeError):
            raise sdbus.DBusFailedError("Error communicating with the audio service.")

    @sdbus.dbus_method_async(
        input_signature="ss",
        result_signature="b",
        flags=sdbus.DbusUnprivilegedFlag,
    )
    async def set_default_sink(self, sink_name: str, card_name: str) -> bool:
        if not self.pulse:
            raise sdbus.DBusFailedError(
                "PipeWire/Pulseaudio is currently not available."
            )

        try:
            # get the sink name by name
            target_sink = next(
                (s for s in self.pulse.sink_list() if s.name == sink_name), None
            )

            # otherwise try to wake the card by using it's name
            if not target_sink and card_name:
                card = self.pulse.get_card_by_name(card_name)
                # Profil setzen (A2DP forcieren)
                profile = next((p for p in card.profile_list if "a2dp" in p.name), None)
                if profile:
                    self.pulse.card_profile_set(card, profile)
                    # Warten, bis PipeWire den Sink erstellt hat
                    loop = asyncio.get_running_loop()
                    new_sink = await loop.run_in_executor(
                        None, lambda: wait_for_new_sink(card.index)
                    )

                    if new_sink:
                        target_sink = new_sink

            if target_sink:
                self.pulse.sink_default_set(target_sink)
                # Streams verschieben (VDR umschalten)
                for stream in self.pulse.sink_input_list():
                    with contextlib.suppress(Exception):
                        self.pulse.sink_input_move(stream.index, target_sink.index)
                return True
            return False

        except (pulsectl.PulseError, AttributeError):
            raise sdbus.DBusFailedError("Error communicating with the audio service.")

    @sdbus.dbus_method_async(
        input_signature="sd",
        result_signature="b",
        flags=sdbus.DbusUnprivilegedFlag,
    )
    async def set_volume(self, sink_name: str, volume: float):
        if not self.pulse:
            raise sdbus.DBusFailedError(
                "PipeWire/Pulseaudio is currently not available."
            )
        try:
            target_sink = next(
                (s for s in self.pulse.sink_list() if s.name == sink_name), None
            )
            if target_sink:
                self.pulse.volume_set_all_chans(target_sink, volume)
                return True
            return False
        except (pulsectl.PulseError, AttributeError):
            raise sdbus.DBusFailedError("Error communicating with the audio service.")


async def monitor_pulse(pulse_interface: PulseDBusControl):
    """Überwacht die PipeWire-Verbindung und verbindet bei Absturz automatisch neu."""
    while True:
        try:
            logging.debug("Connecting to PulseAudio/PipeWire...")
            # Manuelle Instanziierung statt 'with'-Block
            with contextlib.closing(pulsectl.Pulse("pulse_dbus_ctl")) as pulse:
                # Die bestehende D-Bus-Klasse mit der frischen Instanz füttern
                pulse_interface.update_pulse_instance(pulse)
                logging.debug("successfully connected to pipewire.")

                # Endlosschleife zur Verbindungsprüfung (Pulse-Instanz am Leben halten)
                while True:
                    # Test-Aufruf, um zu prüfen, ob PipeWire noch antwortet
                    pulse.server_info()
                    await asyncio.sleep(2)  # Intervall für Heartbeat/Check

        except (pulsectl.PulseError, Exception) as e:
            pulse_interface.update_pulse_instance(None)
            logging.debug(f"PipeWire/pulse connection lost or not available: {e}")
            logging.debug("try reconnect in 3 seconds...")
            await asyncio.sleep(3)


async def main():
    # Open the system bus
    with (
        contextlib.closing(sdbus.sd_bus_open_system()) as system_bus,
    ):
        # Request a name on the system bus
        await system_bus.request_name_async(INTERFACE_NAME, 0)

        # Create and export the interface on the system bus
        pulse_interface = PulseDBusControl(None)
        _pulse_handle = pulse_interface.export_to_dbus(PULSE_OBJECT_PATH, system_bus)

        alsa_interface = alsa.AlsaDBusControl()
        _alsa_handle = alsa_interface.export_to_dbus(alsa.ALSA_OBJECT_PATH, system_bus)

        reconnect_task = asyncio.create_task(monitor_pulse(pulse_interface))

        print("D-Bus service running on the system bus... Press Ctrl+C to stop.")

        # Keep the event loop running
        try:
            await asyncio.Future()
        finally:
            reconnect_task.cancel()
            # do something to keep the references around
            if _alsa_handle == _pulse_handle:
                pass
            # pulse_interface.stop()
            # pulse_handle.stop()  # this prevents a segfault
            # alsa_handle.stop()


def run_main():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run_main()
