#!/usr/bin/env python3
"""
usbtether - USB tethering for macOS (a userspace RNDIS client).

Android's "USB tethering" speaks RNDIS, which macOS has no driver for. This
tool talks RNDIS to the phone directly through libusb, gets an address from
the phone's DHCP server, and exposes the link to macOS as a normal network
interface (a utun device), so the whole Mac - not just proxy-aware apps -
uses the phone's connection, even with Wi-Fi turned off.

    sudo python3 usbtether.py            start tethering (Ctrl-C to stop)
    sudo python3 usbtether.py --list     show USB devices and what was found

Needs: libusb (brew install libusb), pyusb (pip install pyusb), root.
"""

import argparse
import ctypes.util
import fcntl
import os
import random
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

VERBOSE = False
SERVICE_VERSION = "5.0.0"   # keep in sync with the VERSION file (the app compares them)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def dbg(*a):
    if VERBOSE:
        print("  [debug]", *a, flush=True)


# ---------------------------------------------------------------------------
# RNDIS message encoding (pure functions, no USB needed)
# ---------------------------------------------------------------------------

RNDIS_PACKET_MSG = 0x00000001
RNDIS_INITIALIZE_MSG = 0x00000002
RNDIS_HALT_MSG = 0x00000003
RNDIS_QUERY_MSG = 0x00000004
RNDIS_SET_MSG = 0x00000005
RNDIS_INDICATE_STATUS_MSG = 0x00000007
RNDIS_KEEPALIVE_MSG = 0x00000008
RNDIS_CMPLT = 0x80000000
RNDIS_INITIALIZE_CMPLT = RNDIS_INITIALIZE_MSG | RNDIS_CMPLT
RNDIS_QUERY_CMPLT = RNDIS_QUERY_MSG | RNDIS_CMPLT
RNDIS_SET_CMPLT = RNDIS_SET_MSG | RNDIS_CMPLT

OID_GEN_CURRENT_PACKET_FILTER = 0x0001010E
OID_802_3_PERMANENT_ADDRESS = 0x01010101

# directed | multicast | all-multicast | broadcast
PACKET_FILTER = 0x01 | 0x02 | 0x04 | 0x08


def rndis_init_msg(req_id, max_transfer=0x4000):
    return struct.pack("<IIIIII", RNDIS_INITIALIZE_MSG, 24, req_id, 1, 0, max_transfer)


def rndis_query_msg(req_id, oid, info=b""):
    return struct.pack("<IIIIIII", RNDIS_QUERY_MSG, 28 + len(info), req_id,
                       oid, len(info), 20 if info else 0, 0) + info


def rndis_set_msg(req_id, oid, info):
    return struct.pack("<IIIIIII", RNDIS_SET_MSG, 28 + len(info), req_id,
                       oid, len(info), 20, 0) + info


def rndis_keepalive_cmplt(req_id):
    return struct.pack("<IIII", RNDIS_KEEPALIVE_MSG | RNDIS_CMPLT, 16, req_id, 0)


def rndis_halt_msg(req_id):
    return struct.pack("<III", RNDIS_HALT_MSG, 12, req_id)


def rndis_wrap_packet(frame):
    """Wrap one Ethernet frame in an RNDIS data packet header (44 bytes)."""
    hdr = struct.pack("<11I", RNDIS_PACKET_MSG, 44 + len(frame), 36, len(frame),
                      0, 0, 0, 0, 0, 0, 0)
    return hdr + frame


def rndis_unwrap(buf):
    """Return the Ethernet frames contained in one bulk-IN transfer."""
    frames = []
    off = 0
    while off + 8 <= len(buf):
        mtype, mlen = struct.unpack_from("<II", buf, off)
        if mlen < 8 or off + mlen > len(buf):
            break
        if mtype == RNDIS_PACKET_MSG and mlen >= 44:
            doff, dlen = struct.unpack_from("<II", buf, off + 8)
            start = off + 8 + doff
            if dlen > 0 and start + dlen <= off + mlen:
                frames.append(bytes(buf[start:start + dlen]))
        off += mlen
    return frames


# ---------------------------------------------------------------------------
# Ethernet / ARP / IPv4 / UDP / DHCP (pure functions)
# ---------------------------------------------------------------------------

BCAST_MAC = b"\xff" * 6
ZERO_IP = b"\x00" * 4
BCAST_IP = b"\xff" * 4

DHCPDISCOVER, DHCPOFFER, DHCPREQUEST, DHCPACK, DHCPNAK = 1, 2, 3, 5, 6


def inet_checksum(data):
    if len(data) % 2:
        data += b"\x00"
    total = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return (~total) & 0xFFFF


def eth_frame(dst, src, ethertype, payload):
    frame = dst + src + struct.pack("!H", ethertype) + payload
    if len(frame) < 60:
        frame += b"\x00" * (60 - len(frame))
    return frame


def arp_packet(op, smac, sip, tmac, tip):
    return struct.pack("!HHBBH6s4s6s4s", 1, 0x0800, 6, 4, op, smac, sip, tmac, tip)


def parse_arp(payload):
    if len(payload) < 28:
        return None
    _, _, _, _, op, smac, sip, tmac, tip = struct.unpack("!HHBBH6s4s6s4s", payload[:28])
    return {"op": op, "smac": smac, "sip": sip, "tmac": tmac, "tip": tip}


def ipv4_udp(src_ip, dst_ip, sport, dport, payload):
    udp = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload
    hdr = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp),
                      random.getrandbits(16), 0, 64, 17, 0, src_ip, dst_ip)
    hdr = hdr[:10] + struct.pack("!H", inet_checksum(hdr)) + hdr[12:]
    return hdr + udp


def build_dhcp(msg_type, xid, mac, requested_ip=None, server_id=None, ciaddr=ZERO_IP):
    bootp = struct.pack("!BBBBIHH4s4s4s4s16s64s128s",
                        1, 1, 6, 0, xid, 0, 0x8000,
                        ciaddr, ZERO_IP, ZERO_IP, ZERO_IP,
                        mac + b"\x00" * 10, b"\x00" * 64, b"\x00" * 128)
    opts = b"\x63\x82\x53\x63"
    opts += bytes([53, 1, msg_type])
    opts += bytes([61, 7, 1]) + mac
    if requested_ip:
        opts += bytes([50, 4]) + requested_ip
    if server_id:
        opts += bytes([54, 4]) + server_id
    opts += bytes([55, 4, 1, 3, 6, 51])  # subnet, router, dns, lease time
    opts += b"\xff"
    return bootp + opts


def parse_dhcp_reply(frame, xid, mac):
    """Return a dict describing a DHCP server reply for us, or None."""
    if len(frame) < 14 + 20 + 8 + 240 or frame[12:14] != b"\x08\x00":
        return None
    ip = frame[14:]
    if ip[0] >> 4 != 4 or ip[9] != 17:
        return None
    ihl = (ip[0] & 0x0F) * 4
    udp = ip[ihl:]
    sport, dport = struct.unpack("!HH", udp[:4])
    if sport != 67 or dport != 68:
        return None
    b = udp[8:]
    if len(b) < 240 or b[0] != 2:
        return None
    if struct.unpack("!I", b[4:8])[0] != xid or b[28:34] != mac:
        return None
    if b[236:240] != b"\x63\x82\x53\x63":
        return None
    opts = {}
    i = 240
    while i < len(b):
        code = b[i]
        if code == 255:
            break
        if code == 0:
            i += 1
            continue
        if i + 1 >= len(b):
            break
        ln = b[i + 1]
        opts[code] = b[i + 2:i + 2 + ln]
        i += 2 + ln
    if 53 not in opts or not opts[53]:
        return None
    dns_raw = opts.get(6, b"")
    return {
        "msg_type": opts[53][0],
        "yiaddr": b[16:20],
        "src_mac": frame[6:12],
        "src_ip": ip[12:16],
        "subnet": opts.get(1),
        "router": opts[3][:4] if opts.get(3) else None,
        "dns": [dns_raw[k:k + 4] for k in range(0, len(dns_raw) - 3, 4)],
        "lease": struct.unpack("!I", opts[51])[0] if len(opts.get(51, b"")) == 4 else None,
        "server_id": opts[54][:4] if opts.get(54) else ip[12:16],
    }


def is_dhcp_client_packet(ip_packet):
    """True for IPv4 UDP packets addressed to the DHCP client port (68)."""
    if len(ip_packet) < 28 or ip_packet[0] >> 4 != 4 or ip_packet[9] != 17:
        return False
    ihl = (ip_packet[0] & 0x0F) * 4
    if len(ip_packet) < ihl + 4:
        return False
    return struct.unpack("!H", ip_packet[ihl + 2:ihl + 4])[0] == 68


def is_gone(exc):
    """True if the error means the device is really gone (unplugged), not just a glitch."""
    text = str(exc).lower()
    return (getattr(exc, "errno", None) in (19, 2)
            or "no such device" in text or "disconnected" in text
            or "entity not found" in text)


def is_timeout(exc):
    text = str(exc).lower()
    return (type(exc).__name__ in ("USBTimeoutError", "timeout")
            or getattr(exc, "errno", None) in (60, 110)
            or "timeout" in text or "timed out" in text)


# ---------------------------------------------------------------------------
# The RNDIS device (USB side)
# ---------------------------------------------------------------------------

IO_RETRIES = 3  # how often a transient USB error is retried before the link is declared lost


class RndisError(Exception):
    pass


class RndisDevice(object):
    def __init__(self, dev, ctrl_num, ep_in, ep_out, ep_int=None, out_mps=512):
        self.dev = dev
        self.ctrl_num = ctrl_num
        self.ep_in = ep_in
        self.ep_out = ep_out
        self.ep_int = ep_int
        self.out_mps = out_mps or 512
        self.in_size = 32768
        self.max_transfer = 0x4000
        self.mac = None
        self._req_id = 0
        self._write_lock = threading.Lock()
        self.glitches = 0

    def _next_id(self):
        self._req_id += 1
        return self._req_id

    # -- control channel ----------------------------------------------------

    def send_command(self, msg):
        self.dev.ctrl_transfer(0x21, 0x00, 0, self.ctrl_num, msg, 2000)

    def get_response(self, timeout=3.0):
        deadline = time.time() + timeout
        last = None
        while True:
            try:
                data = bytes(self.dev.ctrl_transfer(0xA1, 0x01, 0, self.ctrl_num, 1025, 1000))
                if len(data) >= 8:
                    return data
            except Exception as e:  # not ready yet / stalled
                last = e
            if time.time() > deadline:
                raise RndisError("no response from the phone (%s)" % (last,))
            time.sleep(0.05)

    def handle_misc(self, resp):
        """Answer device keepalives; ignore status indications."""
        mtype = struct.unpack_from("<I", resp, 0)[0]
        if mtype == RNDIS_KEEPALIVE_MSG and len(resp) >= 12:
            self.send_command(rndis_keepalive_cmplt(struct.unpack_from("<I", resp, 8)[0]))
        return mtype

    def transact(self, msg, want_type, req_id):
        self.send_command(msg)
        deadline = time.time() + 5
        while time.time() < deadline:
            resp = self.get_response(max(0.2, deadline - time.time()))
            mtype = self.handle_misc(resp)
            if mtype == want_type and struct.unpack_from("<I", resp, 8)[0] == req_id:
                return resp
            dbg("ignoring RNDIS message 0x%08x" % mtype)
        raise RndisError("timed out waiting for RNDIS reply 0x%08x" % want_type)

    def handshake(self):
        # Throw away any stale reply left over from an earlier session.
        for _ in range(3):
            try:
                self.dev.ctrl_transfer(0xA1, 0x01, 0, self.ctrl_num, 1025, 200)
            except Exception:
                break

        rid = self._next_id()
        resp = self.transact(rndis_init_msg(rid), RNDIS_INITIALIZE_CMPLT, rid)
        status = struct.unpack_from("<I", resp, 12)[0]
        if status != 0:
            raise RndisError("phone rejected RNDIS init (status 0x%08x)" % status)
        if len(resp) >= 40:
            self.max_transfer = struct.unpack_from("<I", resp, 36)[0] or 0x4000
        self.in_size = max(32768, ((self.max_transfer + 2047) // 1024) * 1024)
        dbg("RNDIS initialised, max transfer %d" % self.max_transfer)

        rid = self._next_id()
        resp = self.transact(rndis_query_msg(rid, OID_802_3_PERMANENT_ADDRESS),
                             RNDIS_QUERY_CMPLT, rid)
        status = struct.unpack_from("<I", resp, 12)[0]
        ln, off = struct.unpack_from("<II", resp, 16)
        if status != 0 or ln < 6:
            raise RndisError("could not read the link MAC address (status 0x%08x)" % status)
        self.mac = resp[8 + off:8 + off + 6]

        rid = self._next_id()
        resp = self.transact(rndis_set_msg(rid, OID_GEN_CURRENT_PACKET_FILTER,
                                           struct.pack("<I", PACKET_FILTER)),
                             RNDIS_SET_CMPLT, rid)
        status = struct.unpack_from("<I", resp, 12)[0]
        if status != 0:
            raise RndisError("could not set the packet filter (status 0x%08x)" % status)

    def halt(self):
        try:
            self.send_command(rndis_halt_msg(self._next_id()))
        except Exception:
            pass

    # -- data channel -------------------------------------------------------

    def _io(self, ep, fn):
        """Run one USB transfer. A transient error (the 'Input/Output Error' macOS reports
        now and then) is recovered by clearing the endpoint and trying again, instead of
        dropping the whole link. Timeouts and a vanished device are passed straight up."""
        for attempt in range(IO_RETRIES + 1):
            try:
                return fn()
            except Exception as e:
                if is_timeout(e) or is_gone(e) or attempt == IO_RETRIES:
                    raise
                self.glitches += 1
                if self.glitches <= 25:
                    log("USB glitch (recovering, attempt %d): %s" % (attempt + 1, e))
                try:
                    self.dev.clear_halt(ep.bEndpointAddress)
                except Exception as ce:
                    dbg("clear_halt: %s" % ce)
                time.sleep(0.05 * (attempt + 1))

    def read_frames(self, timeout=1000):
        data = bytes(self._io(self.ep_in, lambda: self.ep_in.read(self.in_size, timeout)))
        return rndis_unwrap(data)

    def write_frame(self, frame):
        msg = rndis_wrap_packet(frame)
        with self._write_lock:
            self._io(self.ep_out, lambda: self.ep_out.write(msg, 2000))
            if len(msg) % self.out_mps == 0:
                # zero-length packet ends the transfer
                self._io(self.ep_out, lambda: self.ep_out.write(b"", 2000))


# ---------------------------------------------------------------------------
# macOS utun interface
# ---------------------------------------------------------------------------

class Utun(object):
    def __init__(self):
        af_system = getattr(socket, "AF_SYSTEM", 32)
        sysproto_control = getattr(socket, "SYSPROTO_CONTROL", 2)
        s = socket.socket(af_system, socket.SOCK_DGRAM, sysproto_control)
        info = struct.pack("I96s", 0, b"com.apple.net.utun_control")
        info = fcntl.ioctl(s, 0xC0644E03, info)  # CTLIOCGINFO
        ctl_id = struct.unpack("I", info[:4])[0]
        s.connect((ctl_id, 0))  # unit 0 = next free utunN
        self.name = s.getsockopt(sysproto_control, 2, 32).split(b"\x00")[0].decode()
        s.settimeout(1.0)
        self.sock = s

    def read(self):
        """Return (address_family, ip_packet)."""
        data = self.sock.recv(65535)
        if len(data) < 5:
            return None, b""
        return struct.unpack("!I", data[:4])[0], data[4:]

    def write(self, ip_packet):
        self.sock.send(struct.pack("!I", socket.AF_INET) + ip_packet)

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def run(cmd, input_text=None):
    dbg("run:", " ".join(cmd))
    p = subprocess.run(cmd, input=input_text, universal_newlines=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        dbg("  failed (%d): %s" % (p.returncode, p.stderr.strip()))
    return p


# ---------------------------------------------------------------------------
# macOS side: utun interface, routes and DNS. Lives longer than one USB session,
# so a short USB drop does not tear down the interface (apps just see a pause).
# ---------------------------------------------------------------------------

class HostNet(object):
    SERVICE_KEY = "usbtether"

    def __init__(self):
        self.tun = None
        self.params = None
        self.added_routes = []
        self.dns_set = False
        self.reconnects = 0

    @property
    def up(self):
        return self.tun is not None

    def configure(self, ip, gw, mask, dns, args):
        """Bring the interface up. Returns False (and changes nothing) if it is already
        up with exactly these settings."""
        params = (ip, gw, mask, tuple(dns), args.mtu, bool(args.no_default_route))
        if self.tun is not None and self.params == params:
            return False
        self.teardown()

        self.tun = Utun()
        ifn = self.tun.name
        ip_s, gw_s, mask_s = socket.inet_ntoa(ip), socket.inet_ntoa(gw), socket.inet_ntoa(mask)
        run(["ifconfig", ifn, "inet", ip_s, gw_s, "netmask", mask_s, "up"])
        run(["ifconfig", ifn, "mtu", str(args.mtu)])

        if not args.no_default_route:
            for net in ("0.0.0.0/1", "128.0.0.0/1"):
                p = run(["route", "-n", "add", "-net", net, gw_s])
                if p.returncode == 0:
                    self.added_routes.append(net)

        dns_s = " ".join(socket.inet_ntoa(d) for d in dns)
        script = "d.init\n"
        script += "d.add ServerAddresses * %s\n" % dns_s
        script += 'd.add SupplementalMatchDomains * ""\n'
        script += "d.add InterfaceName %s\n" % ifn
        script += "set State:/Network/Service/%s/DNS\n" % self.SERVICE_KEY
        script += "d.init\n"
        script += "d.add Addresses * %s\n" % ip_s
        script += "d.add SubnetMasks * %s\n" % mask_s
        script += "d.add DestAddresses * %s\n" % gw_s
        if not args.no_default_route:
            script += "d.add Router %s\n" % gw_s
        script += "d.add InterfaceName %s\n" % ifn
        script += "set State:/Network/Service/%s/IPv4\n" % self.SERVICE_KEY
        run(["scutil"], script)
        self.dns_set = True
        self.params = params
        return True

    def teardown(self):
        for net in self.added_routes:
            run(["route", "-n", "delete", "-net", net])
        self.added_routes = []
        if self.dns_set:
            run(["scutil"], "remove State:/Network/Service/%s/DNS\n"
                            "remove State:/Network/Service/%s/IPv4\n"
                % (self.SERVICE_KEY, self.SERVICE_KEY))
            self.dns_set = False
        if self.tun:
            self.tun.close()
            self.tun = None
        self.params = None


# ---------------------------------------------------------------------------
# The tether: DHCP and the packet pumps for one USB session
# ---------------------------------------------------------------------------

STALL_PROBE_AFTER = 15.0   # seconds of silence before we probe the phone with ARP
STALL_GIVE_UP_AFTER = 7.0  # seconds without any answer to that probe = link is dead
MAX_TX_TIMEOUTS = 5        # consecutive timed-out writes before giving up


class Tether(object):
    def __init__(self, rn, args, host):
        self.rn = rn
        self.args = args
        self.host = host
        self.mac = rn.mac
        self.stop = threading.Event()
        self.threads = []
        self.ip = self.gw = self.mask = None
        self.dns = []
        self.lease = 3600
        self.gw_mac = None
        self.rx_packets = self.tx_packets = 0
        self.rx_bytes = self.tx_bytes = 0
        self.tx_drops = 0
        self.tx_timeouts = 0
        self.last_rx = time.time()
        self.error = None
        self.error_where = ""

    @property
    def tun(self):
        return self.host.tun

    # -- DHCP ---------------------------------------------------------------

    def _send_udp(self, dst_mac, src_ip, dst_ip, sport, dport, payload):
        self.rn.write_frame(eth_frame(dst_mac, self.mac, 0x0800,
                                      ipv4_udp(src_ip, dst_ip, sport, dport, payload)))

    def _wait_dhcp(self, xid, want, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline and not QUIT.is_set():
            try:
                frames = self.rn.read_frames(500)
            except Exception as e:
                if is_timeout(e):
                    continue
                raise
            for f in frames:
                r = parse_dhcp_reply(f, xid, self.mac)
                if r and r["msg_type"] == want:
                    return r
        return None

    def dhcp(self):
        xid = random.getrandbits(32)
        offer = None
        for _ in range(6):
            self._send_udp(BCAST_MAC, ZERO_IP, BCAST_IP, 68, 67,
                           build_dhcp(DHCPDISCOVER, xid, self.mac))
            offer = self._wait_dhcp(xid, DHCPOFFER, 3.0)
            if offer:
                break
            if QUIT.is_set():
                raise RndisError("interrupted")
            log("  waiting for the phone's DHCP server...")
        if not offer:
            raise RndisError("the phone did not answer DHCP. Is USB tethering switched on?")

        self._send_udp(BCAST_MAC, ZERO_IP, BCAST_IP, 68, 67,
                       build_dhcp(DHCPREQUEST, xid, self.mac,
                                  requested_ip=offer["yiaddr"], server_id=offer["server_id"]))
        ack = self._wait_dhcp(xid, DHCPACK, 5.0)
        if not ack:
            raise RndisError("DHCP request was not acknowledged")

        self.ip = ack["yiaddr"]
        self.mask = ack["subnet"] or bytes([255, 255, 255, 0])
        self.gw = ack["router"] or ack["server_id"]
        self.dns = ack["dns"] or [self.gw]
        self.lease = ack["lease"] or 3600
        if ack["server_id"] == self.gw:
            self.gw_mac = ack["src_mac"]
        if self.args.dns:
            self.dns = [socket.inet_aton(d) for d in self.args.dns.split(",")]

    def configure_host(self):
        """Returns True if the macOS interface was (re)configured, False if reused."""
        return self.host.configure(self.ip, self.gw, self.mask, self.dns, self.args)

    # -- packet pumps -----------------------------------------------------------

    def fail(self, exc, where=""):
        if not self.stop.is_set():
            self.error = exc
            self.error_where = where
            self.stop.set()

    def _send(self, frame):
        """Send one Ethernet frame. Returns False if it had to be dropped because the phone
        was slow to accept it (TCP will resend); raises if the link is really broken."""
        try:
            self.rn.write_frame(frame)
            self.tx_timeouts = 0
            return True
        except Exception as e:
            if is_timeout(e) and not is_gone(e):
                self.tx_timeouts += 1
                self.tx_drops += 1
                if self.tx_timeouts >= MAX_TX_TIMEOUTS:
                    raise
                return False
            raise

    def arp_request(self):
        pkt = arp_packet(1, self.mac, self.ip, b"\x00" * 6, self.gw)
        self._send(eth_frame(BCAST_MAC, self.mac, 0x0806, pkt))

    def _handle_arp(self, payload):
        arp = parse_arp(payload)
        if not arp:
            return
        if arp["op"] == 1 and arp["tip"] == self.ip:
            reply = arp_packet(2, self.mac, self.ip, arp["smac"], arp["sip"])
            self._send(eth_frame(arp["smac"], self.mac, 0x0806, reply))
        if arp["sip"] == self.gw and arp["smac"] != b"\x00" * 6:
            self.gw_mac = arp["smac"]

    def rx_loop(self):
        """Phone -> Mac."""
        while not self.stop.is_set():
            try:
                frames = self.rn.read_frames(1000)
            except Exception as e:
                if is_timeout(e):
                    continue
                self.fail(e, "reading from the phone")
                return
            self.last_rx = time.time()
            for f in frames:
                if len(f) < 15:
                    continue
                ethertype = struct.unpack("!H", f[12:14])[0]
                payload = f[14:]
                if ethertype == 0x0800:
                    total = struct.unpack("!H", payload[2:4])[0] if len(payload) >= 4 else 0
                    if 20 <= total <= len(payload):
                        payload = payload[:total]
                    if is_dhcp_client_packet(payload):
                        continue  # reply to our own lease renewal
                    try:
                        self.tun.write(payload)
                        self.rx_packets += 1
                        self.rx_bytes += len(payload)
                    except Exception as e:
                        dbg("tun write failed: %s" % e)
                elif ethertype == 0x0806:
                    try:
                        self._handle_arp(payload)
                    except Exception as e:
                        self.fail(e, "answering ARP")
                        return

    def tx_loop(self):
        """Mac -> phone."""
        while not self.stop.is_set():
            try:
                af, pkt = self.tun.read()
            except socket.timeout:
                continue
            except Exception as e:
                self.fail(e, "reading from the Mac's network interface")
                return
            if af != socket.AF_INET or len(pkt) < 20:
                continue  # IPv6 etc. is not carried (see README)
            if self.gw_mac is None:
                try:
                    self.arp_request()
                except Exception as e:
                    self.fail(e, "writing to the phone")
                    return
                deadline = time.time() + 0.5
                while self.gw_mac is None and time.time() < deadline:
                    time.sleep(0.01)
                if self.gw_mac is None:
                    continue
            try:
                if self._send(eth_frame(self.gw_mac, self.mac, 0x0800, pkt)):
                    self.tx_packets += 1
                    self.tx_bytes += len(pkt)
            except Exception as e:
                self.fail(e, "writing to the phone")
                return

    def ctrl_loop(self):
        """Answer the phone's RNDIS keepalives. Errors here are not fatal by themselves:
        if the phone really needs the answers and stops getting them, the data side fails."""
        if self.rn.ep_int is None:
            return
        errors = 0
        while not self.stop.is_set():
            try:
                self.rn.ep_int.read(8, 1000)
                errors = 0
            except Exception as e:
                if is_timeout(e):
                    continue
                if is_gone(e):
                    self.fail(e, "control channel")
                    return
                errors += 1
                dbg("control channel error (%d): %s" % (errors, e))
                if errors >= 20:
                    dbg("giving up on the control channel")
                    return
                self.stop.wait(0.5)
                continue
            try:
                resp = self.rn.get_response(1.0)
                self.rn.handle_misc(resp)
            except Exception as e:
                dbg("control response: %s" % e)

    def monitor_loop(self):
        """Detect a link that is still plugged in but has stopped carrying data: after a
        quiet spell, ask the phone (ARP) whether it is alive; no answer = reconnect."""
        probe_at = None
        while not self.stop.wait(1.0):
            now = time.time()
            if probe_at is not None:
                if self.last_rx > probe_at:
                    probe_at = None
                elif now - probe_at > STALL_GIVE_UP_AFTER:
                    self.fail(RndisError("the phone stopped responding"), "watchdog")
                    return
            elif now - self.last_rx > STALL_PROBE_AFTER:
                try:
                    self.arp_request()
                except Exception as e:
                    self.fail(e, "writing to the phone")
                    return
                probe_at = now

    def renew_loop(self):
        interval = max(60, min(self.lease // 2, 1800))
        while not self.stop.wait(interval):
            try:
                self._send_udp(BCAST_MAC, self.ip, BCAST_IP, 68, 67,
                               build_dhcp(DHCPREQUEST, random.getrandbits(32),
                                          self.mac, ciaddr=self.ip))
            except Exception as e:
                self.fail(e, "renewing the address")
                return

    def start_threads(self):
        for fn in (self.rx_loop, self.tx_loop, self.ctrl_loop, self.monitor_loop, self.renew_loop):
            t = threading.Thread(target=fn, name=fn.__name__)
            t.daemon = True
            t.start()
            self.threads.append(t)

    def shutdown(self):
        """Stop this USB session. The macOS interface (host) is left alone."""
        self.stop.set()
        for t in self.threads:
            t.join(timeout=3)
        self.rn.halt()


# ---------------------------------------------------------------------------
# USB discovery
# ---------------------------------------------------------------------------

def find_libusb(name):
    for p in ("/opt/homebrew/lib/libusb-1.0.dylib",
              "/usr/local/lib/libusb-1.0.dylib",
              "/opt/local/lib/libusb-1.0.dylib"):
        if os.path.exists(p):
            return p
    return ctypes.util.find_library("usb-1.0")


def load_usb():
    try:
        import usb.core
        import usb.util
        import usb.backend.libusb1
    except ImportError:
        sys.exit("pyusb is not installed. Run:  python3 -m pip install pyusb")
    backend = None
    try:  # a bundled libusb (pip install libusb-package) means no Homebrew is needed
        import libusb_package
        backend = libusb_package.get_libusb1_backend()
    except Exception:
        backend = None
    if backend is None:
        backend = usb.backend.libusb1.get_backend(find_library=find_libusb)
    if backend is None:
        sys.exit("libusb was not found. Install it with:  brew install libusb")
    return usb, backend


RNDIS_SIGNATURES = ((0xE0, 0x01, 0x03), (0x02, 0x02, 0xFF))


def find_rndis_candidates(usb, backend):
    found = []
    for dev in usb.core.find(find_all=True, backend=backend):
        try:
            cfg = dev.get_active_configuration()
        except Exception:
            continue
        ctrl, data = None, []
        for intf in cfg:
            sig = (intf.bInterfaceClass, intf.bInterfaceSubClass, intf.bInterfaceProtocol)
            if sig in RNDIS_SIGNATURES:
                if ctrl is None:
                    ctrl = intf
            elif intf.bInterfaceClass == 0x0A:
                data.append(intf)
        if ctrl is not None and data:
            found.append((dev, ctrl, data))
    return found


def list_devices(usb, backend):
    log("USB devices seen by libusb:")
    for dev in usb.core.find(find_all=True, backend=backend):
        line = "  %04x:%04x" % (dev.idVendor, dev.idProduct)
        try:
            cfg = dev.get_active_configuration()
            sigs = ["%02x/%02x/%02x" % (i.bInterfaceClass, i.bInterfaceSubClass,
                                        i.bInterfaceProtocol) for i in cfg]
            line += "  interfaces (class/sub/proto): " + " ".join(sigs)
        except Exception as e:
            line += "  (could not read configuration: %s)" % e
        log(line)
    log("\nA tethering phone shows an interface 02/02/ff (or e0/01/03) plus 0a/00/00.")


def pick_endpoints(usb, intf):
    ep_in = ep_out = ep_int = None
    for ep in intf:
        direction = usb.util.endpoint_direction(ep.bEndpointAddress)
        kind = usb.util.endpoint_type(ep.bmAttributes)
        if kind == usb.util.ENDPOINT_TYPE_BULK:
            if direction == usb.util.ENDPOINT_IN:
                ep_in = ep
            else:
                ep_out = ep
        elif kind == usb.util.ENDPOINT_TYPE_INTR and direction == usb.util.ENDPOINT_IN:
            ep_int = ep
    return ep_in, ep_out, ep_int


def open_device(usb, dev, ctrl, data_list):
    ep_int = pick_endpoints(usb, ctrl)[2]
    data = ep_in = ep_out = None
    for d in data_list:
        ep_in, ep_out, _ = pick_endpoints(usb, d)
        if ep_in is not None and ep_out is not None:
            data = d
            break
    if data is None:
        raise RndisError("found the RNDIS interface but no bulk data endpoints")
    try:
        for n in sorted(set([ctrl.bInterfaceNumber, data.bInterfaceNumber])):
            usb.util.claim_interface(dev, n)
        if data.bAlternateSetting != 0:
            dev.set_interface_altsetting(interface=data.bInterfaceNumber,
                                         alternate_setting=data.bAlternateSetting)
    except Exception as e:
        raise RndisError(
            "could not claim the USB interface (%s).\n"
            "  - run with sudo\n"
            "  - quit Android File Transfer / Smart Switch, and run 'adb kill-server'\n"
            "  - unplug and re-plug the phone with USB tethering switched on" % e)
    return RndisDevice(dev, ctrl.bInterfaceNumber, ep_in, ep_out, ep_int,
                       ep_out.wMaxPacketSize)


# ---------------------------------------------------------------------------
# status / control files (daemon mode talks to the menu bar app through these)
# ---------------------------------------------------------------------------

QUIT = threading.Event()
CONTROL_DIR = "/Users/Shared/usbtether"
STATUS_FILE = "status.json"
ENABLED_FILE = "enabled"


def ensure_control_dir():
    try:  # the app's installer creates it owned by the user; this is only a fallback
        os.makedirs(CONTROL_DIR, exist_ok=True)
    except OSError as e:
        dbg("could not prepare %s: %s" % (CONTROL_DIR, e))


def daemon_enabled():
    return os.path.exists(os.path.join(CONTROL_DIR, ENABLED_FILE))


def make_status_writer(enabled):
    """Return write(state, detail, **extra); a no-op unless running as the daemon."""
    if not enabled:
        return lambda *a, **k: None
    import json
    ensure_control_dir()
    path = os.path.join(CONTROL_DIR, STATUS_FILE)

    def write(state, detail="", **extra):
        data = {"state": state, "detail": detail, "time": time.time(),
                "version": SERVICE_VERSION}
        data.update(extra)
        tmp = path + ".tmp"
        try:
            # O_NOFOLLOW: the folder is user-owned, so never follow a planted symlink
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except OSError as e:
            dbg("could not write status: %s" % e)
    return write



SETTINGS_FILE = "settings.json"
ANDROID_VENDORS = (0x04E8, 0x18D1, 0x22B8, 0x2717, 0x2A70, 0x12D1, 0x1004, 0x0BB4, 0x054C,
                   0x0FCE, 0x0B05, 0x2916, 0x1BBB, 0x2D95, 0x19D2)


def apply_settings(args):
    """Return a copy of args with the app's settings.json applied (daemon mode)."""
    import copy
    import json
    a = copy.copy(args)
    try:
        with open(os.path.join(CONTROL_DIR, SETTINGS_FILE)) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return a
    if "route_all" in cfg:
        a.no_default_route = not bool(cfg["route_all"])
    dns = str(cfg.get("dns") or "").strip()
    if dns:
        try:
            for d in dns.split(","):
                socket.inet_aton(d.strip())
            a.dns = ",".join(d.strip() for d in dns.split(","))
        except (OSError, ValueError):
            log("Ignoring invalid DNS setting: %r" % dns)
    try:
        mtu = int(cfg.get("mtu", a.mtu))
        if 576 <= mtu <= 1500:
            a.mtu = mtu
    except (TypeError, ValueError):
        pass
    return a


def phone_present(usb, backend):
    """True if an Android-looking device is plugged in (even if tethering is off)."""
    try:
        for dev in usb.core.find(find_all=True, backend=backend):
            if dev.idVendor in ANDROID_VENDORS:
                return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# one tether session
# ---------------------------------------------------------------------------

REQUEST_FILE = "reconnect"
GRACE_SECONDS = 20          # keep the Mac's interface/routes this long while the link is down
NODEVICE_GRACE_SECONDS = 5  # ...but only this long if the phone vanished from USB entirely


def consume_request():
    """True (once) if the menu bar app asked for an immediate reconnect."""
    p = os.path.join(CONTROL_DIR, REQUEST_FILE)
    if os.path.exists(p):
        try:
            os.remove(p)
        except OSError:
            pass
        return True
    return False


def run_session(usb, backend, args, status, daemon, host=None, reset_usb=False):
    """Run one USB session until it ends. Returns (kind, detail):
      'nodevice' - no phone in tethering mode is plugged in
      'failed'   - could not bring the link up
      'lost'     - the link was up and then broke (USB error, stall, unplugged, reconnect asked)
      'ended'    - the link was up and was stopped on purpose (switched off, Ctrl-C)
    The macOS interface lives in `host`; without one, a private one is made and removed here."""
    if daemon:
        args = apply_settings(args)
    try:
        candidates = find_rndis_candidates(usb, backend)
    except Exception as e:  # e.g. the phone is re-enumerating right now
        dbg("USB scan failed: %s" % e)
        candidates = []
    if not candidates:
        return "nodevice", ""

    dev, ctrl, data_list = candidates[0]

    if reset_usb:
        # Last resort after repeated failures: power-cycle the phone's USB link.
        log("Resetting the phone's USB connection...")
        try:
            dev.reset()
        except Exception as e:
            dbg("USB reset: %s" % e)
        try:
            import usb.util
            usb.util.dispose_resources(dev)
        except Exception:
            pass
        return "failed", "USB reset"

    log("Found phone %04x:%04x" % (dev.idVendor, dev.idProduct))
    own_host = host is None
    if own_host:
        host = HostNet()
    status("connecting", "Setting up the USB link...", reconnects=host.reconnects)

    tether = None
    rn = None
    t_up = None
    result = ("ended", "")
    try:
        rn = open_device(usb, dev, ctrl, data_list)
        rn.handshake()
        log("RNDIS link up (MAC %s). Asking the phone for an address..."
            % ":".join("%02x" % b for b in rn.mac))
        tether = Tether(rn, args, host)
        tether.dhcp()
        changed = tether.configure_host()
        ip = socket.inet_ntoa(tether.ip)
        log("%s: %s via %s on %s%s"
            % ("Connected" if changed else "Reconnected (kept network settings)",
               ip, socket.inet_ntoa(tether.gw), host.tun.name,
               "" if not args.no_default_route else " (default route unchanged)"))
        if changed:
            log("DNS: " + ", ".join(socket.inet_ntoa(d) for d in tether.dns))
        log("Press Ctrl-C to stop." if not daemon else "Tethering is up.")
        tether.start_threads()
        t_up = time.time()
        try:
            tether.arp_request()
        except Exception as e:
            dbg("initial ARP failed: %s" % e)

        last = 0.0
        while not tether.stop.is_set() and not QUIT.is_set():
            if daemon and not daemon_enabled():
                log("Switched off from the menu bar.")
                break
            if daemon and consume_request():
                log("Reconnect requested from the menu bar.")
                tether.fail(RndisError("reconnect requested"), "menu bar")
                break
            now = time.time()
            if now - last >= 2:
                status("connected", "Connected", ip=ip, iface=host.tun.name,
                       rx=tether.rx_packets, tx=tether.tx_packets,
                       rx_bytes=tether.rx_bytes, tx_bytes=tether.tx_bytes,
                       since=t_up, glitches=rn.glitches, reconnects=host.reconnects,
                       dropped=tether.tx_drops)
                last = now
            time.sleep(0.25)
        if tether.error is not None:
            log("USB link lost (%s) after %ds: %s"
                % (tether.error_where or "?", time.time() - t_up, tether.error))
            result = ("lost", "%s: %s" % (tether.error_where or "link", tether.error))
    except RndisError as e:
        log("Error: %s" % e)
        result = ("lost" if t_up else "failed", str(e))
    except Exception as e:
        log("Unexpected error: %s" % e)
        if VERBOSE and not daemon:
            raise
        result = ("lost" if t_up else "failed", str(e))
    finally:
        if tether is not None:
            tether.shutdown()
            log("Packets: %d from phone, %d to phone (%d dropped, %d USB glitches recovered)."
                % (tether.rx_packets, tether.tx_packets, tether.tx_drops,
                   rn.glitches if rn else 0))
        if own_host:
            log("Restoring network settings...")
            host.teardown()
        try:
            import usb.util
            usb.util.dispose_resources(dev)
        except Exception:
            pass
    return result


def daemon_loop(usb, backend, args, status):
    """Keep the link up until told to quit: connect when the phone appears, reconnect quickly
    when it drops (keeping the Mac's interface and routes during short drops), and give the
    network back if the phone really went away."""
    log("usbtether daemon started")
    host = HostNet()
    lost_at = None      # when the link last dropped, while we keep the Mac's interface for a bit
    fail_streak = 0     # consecutive failed attempts while the phone is present
    last_state = None
    try:
        while not QUIT.is_set():
            if not daemon_enabled():
                host.teardown()
                lost_at = None
                fail_streak = 0
                status("disabled", "Switched off")
                if last_state != "disabled":
                    log("Tethering is switched off (enable it from the menu bar).")
                last_state = "disabled"
                QUIT.wait(1)
                continue

            reset = fail_streak >= 3
            if reset:
                fail_streak = 0
            try:
                kind, detail = run_session(usb, backend, args, status, True, host, reset)
            except Exception as e:  # never let the service die; launchd's restart is slow
                log("Unexpected error in the tether loop: %s" % e)
                kind, detail = "failed", str(e)
            now = time.time()

            if kind == "lost":
                host.reconnects += 1
                lost_at = now
                fail_streak = 0
                status("reconnecting", "Link dropped - reconnecting...", reconnects=host.reconnects)
                last_state = "reconnecting"
                QUIT.wait(0.5)
                continue
            if kind == "ended":
                host.teardown()
                lost_at = None
                fail_streak = 0
                QUIT.wait(0.5)  # never spin, whatever the reason the session ended
                continue

            # 'nodevice' or 'failed'
            if kind == "failed":
                fail_streak += 1
            limit = NODEVICE_GRACE_SECONDS if kind == "nodevice" else GRACE_SECONDS
            in_grace = host.up and lost_at is not None and now - lost_at <= limit
            if host.up and lost_at is not None and not in_grace:
                log("The phone did not come back; restoring the Mac's network settings.")
                host.teardown()
                lost_at = None

            if in_grace:
                status("reconnecting", "Link dropped - reconnecting...", reconnects=host.reconnects)
                last_state = "reconnecting"
                QUIT.wait(0.5)
            elif kind == "nodevice":
                status("waiting", "Plug in the phone and turn on USB tethering",
                       phone_seen=phone_present(usb, backend), reconnects=host.reconnects)
                if last_state != "waiting":
                    log("Waiting for a phone in USB tethering mode...")
                last_state = "waiting"
                QUIT.wait(2)
            else:
                status("error", detail, reconnects=host.reconnects)
                last_state = "error"
                QUIT.wait(4)
    finally:
        host.teardown()
    status("stopped", "Service stopped")
    log("usbtether daemon stopped")
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    global VERBOSE
    ap = argparse.ArgumentParser(description="USB tethering for macOS (userspace RNDIS)")
    ap.add_argument("--list", action="store_true", help="list USB devices and exit")
    ap.add_argument("--verbose", "-v", action="store_true", help="show debug output")
    ap.add_argument("--daemon", action="store_true",
                    help="run in the background: connect whenever a phone in USB tethering "
                         "mode is plugged in, and reconnect if the link drops "
                         "(used by the menu bar app / launchd)")
    ap.add_argument("--version", action="store_true", help="print the version and exit")
    ap.add_argument("--check", action="store_true",
                    help="check that libusb and pyusb work, then exit")
    ap.add_argument("--no-default-route", action="store_true",
                    help="bring the link up but do not send all traffic through the phone")
    ap.add_argument("--dns", help="comma-separated DNS servers to use instead of the phone's")
    ap.add_argument("--mtu", type=int, default=1500, help="interface MTU (default 1500)")
    args = ap.parse_args()
    VERBOSE = args.verbose
    if args.version:
        print(SERVICE_VERSION)
        return 0

    usb, backend = load_usb()
    if args.check:
        log("libusb OK")
        return 0
    if args.list:
        list_devices(usb, backend)
        return 0

    if sys.platform != "darwin":
        sys.exit("This tool creates a macOS utun interface and only runs on macOS.")
    if os.geteuid() != 0:
        sys.exit("Please run with sudo:  sudo python3 usbtether.py")

    signal.signal(signal.SIGINT, lambda *_: QUIT.set())
    signal.signal(signal.SIGTERM, lambda *_: QUIT.set())
    status = make_status_writer(args.daemon)

    if not args.daemon:
        log("Looking for a phone in USB tethering mode...")
        kind, detail = run_session(usb, backend, args, status, False)
        if kind == "nodevice":
            log("No RNDIS device found.")
            log("  1. Plug in the phone and unlock it.")
            log("  2. Settings > Connections > Mobile Hotspot and Tethering > turn on USB tethering.")
            log("  3. Run again, or run with --list to see what the Mac can see.")
            return 1
        return 0 if kind == "ended" else 1

    return daemon_loop(usb, backend, args, status)


if __name__ == "__main__":
    sys.exit(main())
