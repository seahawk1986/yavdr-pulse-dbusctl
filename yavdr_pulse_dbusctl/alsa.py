from typing import Annotated

import alsaaudio
import sdbus
import logging
import re
from pydantic import BaseModel, BeforeValidator

INTERFACE_NAME = "org.yavdr.AlsaDBusCtl"
ALSA_OBJECT_PATH = "/org/yavdr/PulseDBusCtl/Alsa"


class ProfileSwitchTimeoutError(sdbus.DbusFailedError):
    dbus_error_name = f"{INTERFACE_NAME}.Error.ProfileSwitchTimeout"


class DeviceNotFoundError(sdbus.DbusFailedError):
    dbus_error_name = f"{INTERFACE_NAME}.Error.DeviceNotFound"


FlexibleInt = Annotated[int, BeforeValidator(lambda x: int(x))]


class Mixer(BaseModel):
    name: str
    card_idx: int
    card_name: str
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


def get_mixer_name_id(mixer_name: str) -> tuple[str, int]:
    idx = 0
    name = mixer_name
    if m := re.match(r"(?P<name>\S+)\s(?P<idx>\d+)", mixer_name):
        md = m.groupdict()
        idx = int(md["idx"]) - 1
        name = md["name"]
    return name, idx


def set_mute_state(mixer_name: str, card_idx: int, should_be_muted: bool) -> bool:
    mixer_name, idx = get_mixer_name_id(mixer_name)

    mixer = alsaaudio.Mixer(control=mixer_name, cardindex=card_idx, id=idx)
    try:
        mixer.setmute(int(should_be_muted))
    except alsaaudio.ALSAAudioError:
        return False
    return True


def set_volume(card_idx: int, mixer_name: str, volume: int) -> bool:
    mixer_name, idx = get_mixer_name_id(mixer_name)

    mixer = alsaaudio.Mixer(control=mixer_name, id=idx, cardindex=card_idx)
    try:
        mixer.setvolume(normalize_volume(volume))
        return True
    except alsaaudio.ALSAAudioError:
        return False


def list_mutable_mixers() -> list[Mixer]:
    mixer_data: list[Mixer] = []
    mixer_names: list[str] = alsaaudio.cards()

    for card_idx in alsaaudio.card_indexes():
        name_counts: dict[str, int] = dict()
        for mixer_name in alsaaudio.mixers(card_idx):
            try:
                if mixer_name not in name_counts:
                    name_counts[mixer_name] = 0
                else:
                    name_counts[mixer_name] += 1
                mixer = alsaaudio.Mixer(
                    control=mixer_name, id=name_counts[mixer_name], cardindex=card_idx
                )
                mixer_data.append(
                    Mixer(
                        name=f"{mixer_name}{'' if (idx := name_counts[mixer_name]) == 0 else f' {idx + 1}'}",
                        card_idx=card_idx,
                        card_name=mixer_names[card_idx],
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
        result_signature="a(sisi(ii)b)",
        flags=sdbus.DbusUnprivilegedFlag,
    )
    async def list_alsa_mixers(
        self,
    ) -> list[tuple[str, int, str, int, tuple[int, int], bool]]:
        return [
            (m.name, m.card_idx, m.card_name, m.volume[0], m.volume_range, m.is_muted)
            for m in list_mutable_mixers()
        ]

    @sdbus.dbus_method_async(
        input_signature="siib",
        result_signature="a(sisi(ii)b)",
        flags=sdbus.DbusUnprivilegedFlag,
    )
    async def set_state(
        self,
        mixer_name: str,
        card_idx: int,
        volume: int,
        muted: bool,
    ) -> list[tuple[str, int, str, int, tuple[int, int], bool]]:
        try:
            set_mute_state(
                mixer_name=mixer_name, card_idx=card_idx, should_be_muted=muted
            )
        except Exception:
            logging.exception("could not set mute state for {card_idx}:{mixer_name}")
        try:
            set_volume(mixer_name=mixer_name, card_idx=card_idx, volume=volume)
        except Exception:
            logging.exception(f"could not set volume for {card_idx}:{mixer_name}")
        card_names = alsaaudio.cards()
        r = [
            (
                m.name,
                m.card_idx,
                card_names[m.card_idx],
                m.volume[0],
                m.volume_range,
                m.is_muted,
            )
            for m in list_mutable_mixers()
        ]
        logging.debug(r)
        return r
