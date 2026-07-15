import alsaaudio
import sdbus
import logging
from pydantic import BaseModel

INTERFACE_NAME = "org.yavdr.AlsaDBusCtl"
ALSA_OBJECT_PATH = "/org/yavdr/PulseDBusCtl/Alsa"


class ProfileSwitchTimeoutError(sdbus.DbusFailedError):
    dbus_error_name = f"{INTERFACE_NAME}.Error.ProfileSwitchTimeout"


class DeviceNotFoundError(sdbus.DbusFailedError):
    dbus_error_name = f"{INTERFACE_NAME}.Error.DeviceNotFound"


class Mixer(BaseModel):
    name: str
    card_idx: int
    volume: list[int]
    volume_range: tuple[int, int]
    is_muted: bool


def is_muted(mixer: alsaaudio.Mixer) -> bool:
    return not all(e == 0 for e in mixer.getmute())


def normalize_volume(volume: int) -> int:
    if volume <= 0:
        return 0
    if volume >= 100:
        return 100
    return volume


def set_mute_state(mixer_name: str, card_idx: int, should_be_muted: bool) -> bool:
    mixer = alsaaudio.Mixer(control=mixer_name, cardindex=card_idx)
    try:
        mixer.setmute(int(should_be_muted))
    except alsaaudio.ALSAAudioError:
        return False
    return True


def set_volume(card_idx: int, mixer_name: str, volume: int) -> bool:
    mixer = alsaaudio.Mixer(control=mixer_name, cardindex=card_idx)
    try:
        mixer.setvolume(normalize_volume(volume))
        return True
    except alsaaudio.ALSAAudioError:
        return False


def list_mutable_mixers() -> list[Mixer]:
    mixer_data: list[Mixer] = []

    for card_idx in alsaaudio.card_indexes():
        for mixer_name in alsaaudio.mixers(card_idx):
            try:
                mixer = alsaaudio.Mixer(control=mixer_name, cardindex=card_idx)
                mixer_data.append(
                    Mixer(
                        name=mixer_name,
                        card_idx=card_idx,
                        volume=[
                            normalize_volume(v)
                            for v in mixer.getvolume(alsaaudio.VOLUME_UNITS_PERCENTAGE)
                        ],
                        volume_range=mixer.getrange(
                            alsaaudio.PCM_PLAYBACK, alsaaudio.VOLUME_UNITS_PERCENTAGE
                        ),
                        is_muted=is_muted(mixer),
                    )
                )
            except alsaaudio.ALSAAudioError as err:
                logging.debug(err)
                pass
    logging.debug(*mixer_data, sep="\n")
    return mixer_data


class AlsaDBusControl(sdbus.DbusInterfaceCommonAsync, interface_name=INTERFACE_NAME):
    @sdbus.dbus_method_async(
        result_signature="a(sii(ii)b)",
        flags=sdbus.DbusUnprivilegedFlag,
    )
    async def list_alsa_mixers(
        self,
    ) -> list[tuple[str, int, int, tuple[int, int], bool]]:
        return [
            (m.name, m.card_idx, m.volume[0], m.volume_range, m.is_muted)
            for m in list_mutable_mixers()
        ]

    @sdbus.dbus_method_async(
        input_signature="siib",
        result_signature="a(sii(ii)b)",
        flags=sdbus.DbusUnprivilegedFlag,
    )
    async def set_state(
        self,
        mixer_name: str,
        card_idx: int,
        volume: int,
        muted: bool,
    ) -> list[tuple[str, int, int, tuple[int, int], bool]]:
        set_mute_state(mixer_name=mixer_name, card_idx=card_idx, should_be_muted=muted)
        set_volume(mixer_name=mixer_name, card_idx=card_idx, volume=volume)
        r = [
            (m.name, m.card_idx, m.volume[0], m.volume_range, m.is_muted)
            for m in list_mutable_mixers()
        ]
        logging.debug(r)
        return r
