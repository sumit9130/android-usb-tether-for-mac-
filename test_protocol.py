#!/usr/bin/env python3
"""Self-tests for the pure protocol code (no phone or root needed): python3 test_protocol.py"""
import struct
import unittest

import usbtether as ut

MAC = bytes.fromhex("021122334455")
SRV_MAC = bytes.fromhex("aabbccddeeff")
SRV_IP = bytes([192, 168, 42, 129])
CLI_IP = bytes([192, 168, 42, 77])


class RndisTests(unittest.TestCase):
    def test_init_msg_layout(self):
        m = ut.rndis_init_msg(7)
        self.assertEqual(len(m), 24)
        t, ln, rid, major, minor, mx = struct.unpack("<IIIIII", m)
        self.assertEqual((t, ln, rid, major, minor), (2, 24, 7, 1, 0))

    def test_query_msg_length_field(self):
        m = ut.rndis_query_msg(3, ut.OID_802_3_PERMANENT_ADDRESS)
        self.assertEqual(len(m), 28)
        self.assertEqual(struct.unpack_from("<I", m, 4)[0], 28)

    def test_set_msg_carries_info(self):
        m = ut.rndis_set_msg(4, ut.OID_GEN_CURRENT_PACKET_FILTER, struct.pack("<I", 0x0F))
        self.assertEqual(len(m), 32)
        self.assertEqual(struct.unpack_from("<I", m, 4)[0], 32)
        self.assertEqual(struct.unpack_from("<I", m, 16)[0], 4)    # info length
        self.assertEqual(struct.unpack_from("<I", m, 20)[0], 20)   # info offset
        self.assertEqual(m[28:], struct.pack("<I", 0x0F))

    def test_wrap_unwrap_roundtrip(self):
        frame = bytes(range(100))
        self.assertEqual(ut.rndis_unwrap(ut.rndis_wrap_packet(frame)), [frame])

    def test_unwrap_multiple_packets_in_one_transfer(self):
        a, b = b"A" * 60, b"B" * 77
        buf = ut.rndis_wrap_packet(a) + ut.rndis_wrap_packet(b)
        self.assertEqual(ut.rndis_unwrap(buf), [a, b])

    def test_unwrap_ignores_garbage_and_truncation(self):
        good = ut.rndis_wrap_packet(b"X" * 60)
        self.assertEqual(ut.rndis_unwrap(good[:-5]), [])
        self.assertEqual(ut.rndis_unwrap(b"\x00" * 3), [])
        self.assertEqual(ut.rndis_unwrap(struct.pack("<II", 1, 4)), [])


class NetTests(unittest.TestCase):
    def test_checksum_known_vector(self):
        # RFC 1071 example data
        data = bytes.fromhex("0001f203f4f5f6f7")
        self.assertEqual(ut.inet_checksum(data), 0x220D)

    def test_ipv4_header_checksum_validates(self):
        pkt = ut.ipv4_udp(ut.ZERO_IP, ut.BCAST_IP, 68, 67, b"hello")
        self.assertEqual(ut.inet_checksum(pkt[:20]), 0)

    def test_eth_frame_padded_to_60(self):
        f = ut.eth_frame(ut.BCAST_MAC, MAC, 0x0806, b"x")
        self.assertEqual(len(f), 60)

    def test_arp_roundtrip(self):
        p = ut.arp_packet(1, MAC, CLI_IP, b"\x00" * 6, SRV_IP)
        a = ut.parse_arp(p + b"\x00" * 18)
        self.assertEqual((a["op"], a["smac"], a["sip"], a["tip"]), (1, MAC, CLI_IP, SRV_IP))


def make_server_reply(msg_type, xid, with_dns=True):
    bootp = struct.pack("!BBBBIHH4s4s4s4s16s64s128s", 2, 1, 6, 0, xid, 0, 0,
                        ut.ZERO_IP, CLI_IP, ut.ZERO_IP, ut.ZERO_IP,
                        MAC + b"\x00" * 10, b"\x00" * 64, b"\x00" * 128)
    opts = b"\x63\x82\x53\x63" + bytes([53, 1, msg_type])
    opts += bytes([54, 4]) + SRV_IP
    opts += bytes([1, 4, 255, 255, 255, 0])
    opts += bytes([3, 4]) + SRV_IP
    if with_dns:
        opts += bytes([6, 8]) + SRV_IP + bytes([8, 8, 8, 8])
    opts += bytes([51, 4]) + struct.pack("!I", 3600) + b"\xff"
    ip = ut.ipv4_udp(SRV_IP, ut.BCAST_IP, 67, 68, bootp + opts)
    return ut.eth_frame(MAC, SRV_MAC, 0x0800, ip)


class DhcpTests(unittest.TestCase):
    def test_discover_is_well_formed(self):
        d = ut.build_dhcp(ut.DHCPDISCOVER, 0x1234, MAC)
        self.assertEqual(d[0], 1)
        self.assertEqual(struct.unpack("!I", d[4:8])[0], 0x1234)
        self.assertEqual(d[28:34], MAC)
        self.assertEqual(d[236:240], b"\x63\x82\x53\x63")
        self.assertEqual(d[240:243], bytes([53, 1, 1]))
        self.assertTrue(d.endswith(b"\xff"))

    def test_request_includes_requested_ip_and_server(self):
        d = ut.build_dhcp(ut.DHCPREQUEST, 1, MAC, requested_ip=CLI_IP, server_id=SRV_IP)
        self.assertIn(bytes([50, 4]) + CLI_IP, d)
        self.assertIn(bytes([54, 4]) + SRV_IP, d)

    def test_parse_offer(self):
        r = ut.parse_dhcp_reply(make_server_reply(ut.DHCPOFFER, 99, True), 99, MAC)
        self.assertEqual(r["msg_type"], ut.DHCPOFFER)
        self.assertEqual(r["yiaddr"], CLI_IP)
        self.assertEqual(r["router"], SRV_IP)
        self.assertEqual(r["server_id"], SRV_IP)
        self.assertEqual(r["src_mac"], SRV_MAC)
        self.assertEqual(r["dns"], [SRV_IP, bytes([8, 8, 8, 8])])
        self.assertEqual(r["lease"], 3600)
        self.assertEqual(r["subnet"], bytes([255, 255, 255, 0]))

    def test_wrong_xid_or_mac_is_ignored(self):
        f = make_server_reply(ut.DHCPACK, 5)
        self.assertIsNone(ut.parse_dhcp_reply(f, 6, MAC))
        self.assertIsNone(ut.parse_dhcp_reply(f, 5, bytes(6)))

    def test_non_dhcp_frames_are_ignored(self):
        arp = ut.eth_frame(ut.BCAST_MAC, SRV_MAC, 0x0806, b"\x00" * 28)
        self.assertIsNone(ut.parse_dhcp_reply(arp, 1, MAC))
        self.assertIsNone(ut.parse_dhcp_reply(b"short", 1, MAC))

    def test_offer_without_dns_option(self):
        r = ut.parse_dhcp_reply(make_server_reply(ut.DHCPACK, 3, False), 3, MAC)
        self.assertEqual(r["dns"], [])

    def test_dhcp_client_packet_detection(self):
        pkt = ut.ipv4_udp(SRV_IP, CLI_IP, 67, 68, b"x" * 10)
        self.assertTrue(ut.is_dhcp_client_packet(pkt))
        self.assertFalse(ut.is_dhcp_client_packet(ut.ipv4_udp(SRV_IP, CLI_IP, 53, 5353, b"x")))


class MiscTests(unittest.TestCase):
    def test_timeout_detection(self):
        class USBTimeoutError(Exception):
            pass
        self.assertTrue(ut.is_timeout(USBTimeoutError("x")))
        self.assertTrue(ut.is_timeout(Exception("Operation timed out")))
        self.assertFalse(ut.is_timeout(Exception("No such device (it may have been disconnected)")))


class DaemonFileTests(unittest.TestCase):
    def test_status_writer_and_enabled_flag(self):
        import json, os, tempfile
        d = tempfile.mkdtemp()
        ut.CONTROL_DIR = d
        self.assertFalse(ut.daemon_enabled())
        open(os.path.join(d, ut.ENABLED_FILE), "w").close()
        self.assertTrue(ut.daemon_enabled())
        w = ut.make_status_writer(True)
        w("connected", "Connected", ip="10.0.0.2", iface="utun4", rx=5, tx=6)
        data = json.load(open(os.path.join(d, ut.STATUS_FILE)))
        self.assertEqual((data["state"], data["ip"], data["rx"]), ("connected", "10.0.0.2", 5))
        self.assertAlmostEqual(data["time"], __import__("time").time(), delta=5)

    def test_apply_settings(self):
        import argparse, json, os, tempfile
        d = tempfile.mkdtemp()
        ut.CONTROL_DIR = d
        base = argparse.Namespace(no_default_route=False, dns=None, mtu=1500)
        self.assertEqual(ut.apply_settings(base).mtu, 1500)  # no file: unchanged
        json.dump({"route_all": False, "dns": "1.1.1.1, 8.8.8.8", "mtu": 1400},
                  open(os.path.join(d, ut.SETTINGS_FILE), "w"))
        a = ut.apply_settings(base)
        self.assertTrue(a.no_default_route)
        self.assertEqual(a.dns, "1.1.1.1,8.8.8.8")
        self.assertEqual(a.mtu, 1400)
        self.assertFalse(base.no_default_route)  # original untouched
        json.dump({"dns": "not-an-ip", "mtu": 99999},
                  open(os.path.join(d, ut.SETTINGS_FILE), "w"))
        b = ut.apply_settings(base)
        self.assertIsNone(b.dns)
        self.assertEqual(b.mtu, 1500)

    def test_status_writer_is_noop_outside_daemon(self):
        self.assertIsNone(ut.make_status_writer(False)("x"))


class FakeUSBError(Exception):
    def __init__(self, errno, msg):
        Exception.__init__(self, "[Errno %d] %s" % (errno, msg))
        self.errno = errno


class FakeEp(object):
    def __init__(self, script):
        self.script = list(script)   # items: bytes to return, or an Exception to raise
        self.bEndpointAddress = 0x81
        self.calls = 0

    def read(self, size, timeout):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    write = read


class FakeDev(object):
    def __init__(self):
        self.halts = []

    def clear_halt(self, addr):
        self.halts.append(addr)


def make_rn(ep_in=None, ep_out=None):
    rn = ut.RndisDevice(FakeDev(), 0, ep_in, ep_out, None, 512)
    rn.mac = MAC
    return rn


class ResilienceTests(unittest.TestCase):
    def test_is_gone(self):
        self.assertTrue(ut.is_gone(FakeUSBError(19, "No such device (it may have been disconnected)")))
        self.assertTrue(ut.is_gone(FakeUSBError(2, "Entity not found")))
        self.assertFalse(ut.is_gone(FakeUSBError(5, "Input/Output Error")))
        self.assertFalse(ut.is_gone(FakeUSBError(60, "Operation timed out")))

    def test_transient_io_error_is_retried_not_fatal(self):
        frame = ut.rndis_wrap_packet(b"X" * 60)
        ep = FakeEp([FakeUSBError(5, "Input/Output Error"),
                     FakeUSBError(5, "Input/Output Error"), frame])
        rn = make_rn(ep_in=ep)
        self.assertEqual(rn.read_frames(100), [b"X" * 60])
        self.assertEqual(rn.glitches, 2)
        self.assertEqual(rn.dev.halts, [0x81, 0x81])   # endpoint was cleared before each retry

    def test_persistent_io_error_gives_up(self):
        ep = FakeEp([FakeUSBError(5, "Input/Output Error")] * (ut.IO_RETRIES + 1))
        rn = make_rn(ep_in=ep)
        with self.assertRaises(FakeUSBError):
            rn.read_frames(100)
        self.assertEqual(ep.calls, ut.IO_RETRIES + 1)

    def test_timeout_and_unplug_are_not_retried(self):
        for err in (FakeUSBError(60, "Operation timed out"),
                    FakeUSBError(19, "No such device (it may have been disconnected)")):
            ep = FakeEp([err])
            rn = make_rn(ep_in=ep)
            with self.assertRaises(FakeUSBError):
                rn.read_frames(100)
            self.assertEqual(ep.calls, 1)
            self.assertEqual(rn.glitches, 0)

    def test_write_retries_too(self):
        ep = FakeEp([FakeUSBError(32, "Pipe error"), b""])
        rn = make_rn(ep_out=ep)
        rn.write_frame(b"A" * 61)     # wrapped length is not a multiple of 512: no ZLP
        self.assertEqual(rn.glitches, 1)

    def _tether(self, rn):
        host = ut.HostNet()
        t = ut.Tether(rn, __import__("argparse").Namespace(dns=None, mtu=1500, no_default_route=False), host)
        t.ip, t.gw, t.gw_mac = CLI_IP, SRV_IP, SRV_MAC
        return t

    def test_slow_phone_drops_packets_but_keeps_link(self):
        ep = FakeEp([FakeUSBError(60, "Operation timed out")] * 3 + [b""])
        t = self._tether(make_rn(ep_out=ep))
        for _ in range(3):
            self.assertFalse(t._send(b"F" * 60))
        self.assertEqual(t.tx_drops, 3)
        self.assertTrue(t._send(b"F" * 60))
        self.assertEqual(t.tx_timeouts, 0)    # a success resets the count

    def test_too_many_timeouts_fail_the_link(self):
        ep = FakeEp([FakeUSBError(60, "Operation timed out")] * ut.MAX_TX_TIMEOUTS)
        t = self._tether(make_rn(ep_out=ep))
        for _ in range(ut.MAX_TX_TIMEOUTS - 1):
            self.assertFalse(t._send(b"F" * 60))
        with self.assertRaises(FakeUSBError):
            t._send(b"F" * 60)

    def test_watchdog_declares_silent_link_dead(self):
        import time
        old = (ut.STALL_PROBE_AFTER, ut.STALL_GIVE_UP_AFTER)
        ut.STALL_PROBE_AFTER, ut.STALL_GIVE_UP_AFTER = 0.2, 0.5
        try:
            ep_out = FakeEp([b""] * 50)           # probes are accepted, nothing ever comes back
            t = self._tether(make_rn(ep_out=ep_out))
            t.last_rx = time.time()
            th = __import__("threading").Thread(target=t.monitor_loop)
            th.daemon = True
            th.start()
            th.join(5)
            self.assertTrue(t.stop.is_set())
            self.assertEqual(t.error_where, "watchdog")
            self.assertGreaterEqual(ep_out.calls, 1)   # it did send an ARP probe first
        finally:
            ut.STALL_PROBE_AFTER, ut.STALL_GIVE_UP_AFTER = old

    def test_watchdog_stays_quiet_when_phone_answers(self):
        import time
        old = (ut.STALL_PROBE_AFTER, ut.STALL_GIVE_UP_AFTER)
        ut.STALL_PROBE_AFTER, ut.STALL_GIVE_UP_AFTER = 0.2, 0.8
        try:
            t = self._tether(make_rn(ep_out=FakeEp([b""] * 50)))
            t.last_rx = time.time()
            th = __import__("threading").Thread(target=t.monitor_loop)
            th.daemon = True
            th.start()
            end = time.time() + 2.5
            while time.time() < end:          # the phone keeps answering
                t.last_rx = time.time()
                time.sleep(0.1)
            self.assertFalse(t.stop.is_set())
            t.stop.set()
            th.join(3)
        finally:
            ut.STALL_PROBE_AFTER, ut.STALL_GIVE_UP_AFTER = old


class HostNetTests(unittest.TestCase):
    def test_same_settings_keep_interface_changed_settings_rebuild(self):
        import argparse
        calls = []

        class FakeTun(object):
            name = "utun9"
            closed = False

            def close(self):
                self.closed = True

        orig_tun, orig_run = ut.Utun, ut.run
        ut.Utun = FakeTun
        ut.run = lambda cmd, input_text=None: calls.append(cmd) or type("P", (), {"returncode": 0})()
        try:
            args = argparse.Namespace(mtu=1500, no_default_route=False)
            host = ut.HostNet()
            mask = bytes([255, 255, 255, 0])
            self.assertTrue(host.configure(CLI_IP, SRV_IP, mask, [SRV_IP], args))
            first_tun, n = host.tun, len(calls)
            self.assertTrue(host.up)
            # phone reconnects with the same address: nothing is touched
            self.assertFalse(host.configure(CLI_IP, SRV_IP, mask, [SRV_IP], args))
            self.assertIs(host.tun, first_tun)
            self.assertEqual(len(calls), n)
            # different address: interface is rebuilt
            self.assertTrue(host.configure(bytes([192, 168, 42, 99]), SRV_IP, mask, [SRV_IP], args))
            self.assertTrue(first_tun.closed)
            host.teardown()
            self.assertFalse(host.up)
        finally:
            ut.Utun, ut.run = orig_tun, orig_run


class FakeClock(object):
    """Stands in for the time module so grace periods can be tested instantly."""
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s

    def strftime(self, fmt):
        return "00:00:00"


class FakeQuit(object):
    def __init__(self, clock, max_waits):
        self.clock, self.max_waits, self.waits, self._set = clock, max_waits, 0, False

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    def wait(self, t=None):
        self.clock.now += t or 0
        self.waits += 1
        if self.waits >= self.max_waits:
            self._set = True
        return self._set


class DaemonLoopTests(unittest.TestCase):
    def run_loop(self, script, max_waits=20):
        """script: list of (seconds_to_pass, kind, detail, host_up_after) per run_session call."""
        import argparse
        clock = FakeClock()
        saved = (ut.time, ut.QUIT, ut.run_session, ut.daemon_enabled, ut.phone_present)
        calls, statuses = [], []
        state = {"i": 0}

        def fake_run_session(usb, backend, args, status, daemon, host=None, reset_usb=False):
            calls.append({"reset": reset_usb, "host_up": host.up, "t": clock.now})
            step = script[min(state["i"], len(script) - 1)]
            state["i"] += 1
            clock.now += step[0]
            if step[3] and not host.up:
                host.tun = object()          # pretend the interface came up
                host.close = None
            return step[1], step[2]

        class T(object):
            closed = 0

        orig_teardown = ut.HostNet.teardown

        def fake_teardown(self):
            self.tun = None
            self.params = None

        ut.time, ut.QUIT = clock, FakeQuit(clock, max_waits)
        ut.run_session = fake_run_session
        ut.daemon_enabled = lambda: True
        ut.phone_present = lambda *a: True
        ut.HostNet.teardown = fake_teardown
        try:
            ut.daemon_loop(None, None, argparse.Namespace(), lambda st, d="", **k: statuses.append((st, k)))
        finally:
            ut.time, ut.QUIT, ut.run_session, ut.daemon_enabled, ut.phone_present = saved
            ut.HostNet.teardown = orig_teardown
        return calls, statuses

    def test_brief_drop_keeps_interface_and_reconnects(self):
        calls, statuses = self.run_loop([
            (60, "lost", "reading from the phone: EIO", True),   # was up for a minute, then dropped
            (0, "lost", "again", True),
            (0, "ended", "", False)], max_waits=6)
        # the second attempt started with the interface still up (nothing was torn down)
        self.assertTrue(calls[1]["host_up"])
        self.assertTrue(any(st == "reconnecting" and k.get("reconnects") == 1 for st, k in statuses))

    def test_phone_that_never_returns_gives_the_network_back(self):
        calls, statuses = self.run_loop([
            (10, "lost", "x", True),
            (3, "nodevice", "", False),     # 3 s later: still within the 5 s allowance
            (3, "nodevice", "", False),     # 6 s later: too long
            (0, "nodevice", "", False)], max_waits=8)
        self.assertTrue(calls[1]["host_up"])
        self.assertTrue(calls[2]["host_up"])
        self.assertFalse(calls[3]["host_up"])           # torn down after the allowance
        self.assertEqual(statuses[-2][0] if statuses[-1][0] == "stopped" else statuses[-1][0], "waiting")

    def test_repeated_failures_escalate_to_usb_reset(self):
        calls, _ = self.run_loop([(0, "failed", "boom", False)] * 5, max_waits=6)
        resets = [c["reset"] for c in calls]
        self.assertEqual(resets[:3], [False, False, False])
        self.assertTrue(resets[3])      # fourth attempt resets USB first


class SessionTests(unittest.TestCase):
    """Drives run_session end to end with a fake phone: a drop is reported as 'lost' and a
    second session reuses the same macOS interface."""

    def test_drop_then_reconnect_reuses_interface(self):
        import argparse, time as _time

        class FakeTun(object):
            name = "utun9"

            def read(self):
                _time.sleep(0.05)
                raise __import__("socket").timeout()

            def write(self, p):
                pass

            def close(self):
                pass

        class FakeRn(object):
            mac = MAC
            ep_int = None
            glitches = 0

            def __init__(self, fail_after):
                self.t0 = _time.time()
                self.fail_after = fail_after

            def handshake(self):
                pass

            def read_frames(self, timeout=1000):
                _time.sleep(0.05)
                if self.fail_after is not None and _time.time() - self.t0 > self.fail_after:
                    raise FakeUSBError(5, "Input/Output Error")
                return []

            def write_frame(self, f):
                pass

            def halt(self):
                pass

        class FakeDevice(object):
            idVendor, idProduct = 0x04E8, 0x6863

        rns = [FakeRn(0.4), FakeRn(None)]
        saved = (ut.find_rndis_candidates, ut.open_device, ut.Tether.dhcp, ut.Utun, ut.run, ut.QUIT)
        ut.find_rndis_candidates = lambda usb, backend: [(FakeDevice(), None, [])]
        ut.open_device = lambda *a: rns.pop(0)

        def fake_dhcp(self):
            self.ip, self.gw, self.mask = CLI_IP, SRV_IP, bytes([255, 255, 255, 0])
            self.dns, self.gw_mac = [SRV_IP], SRV_MAC

        ut.Tether.dhcp = fake_dhcp
        ut.Utun = FakeTun
        ut.run = lambda cmd, input_text=None: type("P", (), {"returncode": 0})()
        quit_ev = __import__("threading").Event()
        ut.QUIT = quit_ev
        statuses = []
        args = argparse.Namespace(dns=None, mtu=1500, no_default_route=False)
        host = ut.HostNet()
        try:
            kind, detail = ut.run_session(None, None, args, lambda st, d="", **k: statuses.append(st),
                                          False, host)
            self.assertEqual(kind, "lost")
            self.assertIn("reading from the phone", detail)
            self.assertTrue(host.up)                      # interface survived the drop
            first_tun = host.tun
            __import__("threading").Timer(0.6, quit_ev.set).start()
            kind, detail = ut.run_session(None, None, args, lambda st, d="", **k: statuses.append(st),
                                          False, host)
            self.assertEqual(kind, "ended")
            self.assertIs(host.tun, first_tun)            # same interface, not rebuilt
            self.assertIn("connected", statuses)
        finally:
            (ut.find_rndis_candidates, ut.open_device, ut.Tether.dhcp, ut.Utun, ut.run,
             ut.QUIT) = saved


class VersionTests(unittest.TestCase):
    def test_version_matches_version_file_and_is_in_status(self):
        import json, os, tempfile
        here = os.path.dirname(os.path.abspath(__file__))
        self.assertEqual(ut.SERVICE_VERSION, open(os.path.join(here, "VERSION")).read().strip())
        d = tempfile.mkdtemp()
        ut.CONTROL_DIR = d
        ut.make_status_writer(True)("waiting")
        with open(os.path.join(d, ut.STATUS_FILE)) as f:
            self.assertEqual(json.load(f)["version"], ut.SERVICE_VERSION)


if __name__ == "__main__":
    unittest.main(verbosity=2)
