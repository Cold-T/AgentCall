"""Adapted libc SCO connector from handsfree-linux; see vendor license."""

import ctypes as _ct
import logging
import struct as _struct

logger = logging.getLogger(__name__)

_AF_BLUETOOTH = 31
_SOCK_SEQPACKET = 5
_BTPROTO_SCO = 2
_SOL_SOCKET = 1
_SO_SNDTIMEO = 13
_SOL_BLUETOOTH = 274
_BT_VOICE = 11
# Voice settings: kernel passes raw bytes (mSBC) vs. converts CVSD↔PCM
_BT_VOICE_TRANSPARENT = 0x0003
_BT_VOICE_CVSD_16BIT = 0x0060


class _sockaddr_sco(_ct.Structure):
    _fields_ = [
        ("sco_family", _ct.c_uint16),
        ("sco_bdaddr", _ct.c_uint8 * 6),
    ]


def _mac_to_bdaddr(mac: str):
    octets = [int(x, 16) for x in mac.split(":")]
    octets.reverse()  # bdaddr is little-endian
    return (_ct.c_uint8 * 6)(*octets)


def _load_libc():
    """Load libc via ctypes, works on x86-64, ARM (Pi), aarch64, etc."""
    from ctypes.util import find_library

    # find_library("c") returns "libc.so.6" on glibc, "libc.musl-*.so.1" on musl (Alpine)
    name = find_library("c") or "libc.so.6"
    return _ct.CDLL(name, use_errno=True)


def _sco_connect(
    remote_mac: str,
    timeout_sec: float = 2.0,
    voice_setting: int = _BT_VOICE_CVSD_16BIT,
    local_mac: str = "00:00:00:00:00:00",
    raise_errors: bool = False,
) -> int:
    """
    Open and connect a BTPROTO_SCO socket via libc.
    Returns raw fd on success, -1 on failure; raise_errors preserves OS error details.

    voice_setting controls what the kernel does with the SCO data:
      _BT_VOICE_CVSD_16BIT  (0x0060) — kernel converts CVSD↔16-bit PCM (CVSD calls)
      _BT_VOICE_TRANSPARENT (0x0003) — kernel passes raw bytes through (mSBC calls)

    Transparent mode failure is an error; the caller must not change codec
    without matching HFP negotiation.
    """
    libc = None
    fd = -1
    try:
        libc = _load_libc()
        fd = libc.socket(_AF_BLUETOOTH, _SOCK_SEQPACKET, _BTPROTO_SCO)
        if fd < 0:
            raise OSError(_ct.get_errno(), "SCO socket failed")
        vs = _ct.c_uint16(voice_setting)
        if libc.setsockopt(fd, _SOL_BLUETOOTH, _BT_VOICE, _ct.byref(vs), 2) < 0:
            raise OSError(_ct.get_errno(), "SCO BT_VOICE failed")
        local = _sockaddr_sco(_AF_BLUETOOTH, _mac_to_bdaddr(local_mac))
        if libc.bind(fd, _ct.byref(local), _ct.sizeof(local)) < 0:
            raise OSError(_ct.get_errno(), "SCO bind failed")
        if timeout_sec <= 0:
            raise ValueError("SCO connect timeout must be positive")
        seconds = int(timeout_sec)
        tv = _struct.pack("@ll", seconds, max(1, int((timeout_sec - seconds) * 1_000_000)))
        tv_buf = (_ct.c_char * len(tv))(*tv)
        if libc.setsockopt(fd, _SOL_SOCKET, _SO_SNDTIMEO, tv_buf, len(tv)) < 0:
            raise OSError(_ct.get_errno(), "SCO timeout setup failed")
        remote = _sockaddr_sco(_AF_BLUETOOTH, _mac_to_bdaddr(remote_mac))
        if libc.connect(fd, _ct.byref(remote), _ct.sizeof(remote)) < 0:
            raise OSError(_ct.get_errno(), "SCO connect failed")
        result = fd
        fd = -1
        return result
    except Exception:
        if raise_errors:
            raise
        logger.debug("SCO connector failed", exc_info=True)
        return -1
    finally:
        if fd >= 0 and libc is not None:
            libc.close(fd)


def sco_listen(local_mac, voice_setting):
    """Listen for phone-initiated SCO; the returned fd is owned by the caller."""
    libc = _load_libc()
    fd = libc.socket(_AF_BLUETOOTH, _SOCK_SEQPACKET, _BTPROTO_SCO)
    if fd < 0:
        raise OSError(_ct.get_errno(), "SCO socket failed")
    try:
        vs = _ct.c_uint16(voice_setting)
        local = _sockaddr_sco(_AF_BLUETOOTH, _mac_to_bdaddr(local_mac))
        if libc.setsockopt(fd, _SOL_BLUETOOTH, _BT_VOICE, _ct.byref(vs), 2) < 0:
            raise OSError(_ct.get_errno(), "BT_VOICE failed")
        if libc.bind(fd, _ct.byref(local), _ct.sizeof(local)) < 0:
            raise OSError(_ct.get_errno(), "SCO bind failed")
        if libc.listen(fd, 5) < 0:
            raise OSError(_ct.get_errno(), "SCO listen failed")
        return fd
    except BaseException:
        libc.close(fd)
        raise
