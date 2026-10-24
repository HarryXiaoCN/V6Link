"""Security and encrypted-storage tests."""

from __future__ import annotations

import asyncio
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from v6link.crypto import (
    Identity,
    client_handshake,
    server_handshake,
)
from v6link.errors import IdentityMismatch, VaultError
from v6link.filestore import (
    HEADER_SIZE,
    AttachmentWriter,
    iter_plaintext,
)
from v6link.netutil import (
    LocalAddress,
    local_ipv6_addresses,
    parse_endpoint,
    sanitize_filename,
    shareable_ipv6_addresses,
)
from v6link.sharecard import parse_share_card
from v6link.vault import Vault


def _ipv6_loopback_available() -> bool:
    if os.environ.get("V6LINK_SKIP_E2E"):
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::", 0))
        return True
    except OSError:
        return False


IPV6_LOOPBACK = _ipv6_loopback_available()


class RecordingWriter:
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.captured = bytearray()

    def write(self, data):
        self.captured.extend(data)
        return self.wrapped.write(data)

    async def drain(self):
        return await self.wrapped.drain()

    def close(self):
        return self.wrapped.close()

    async def wait_closed(self):
        return await self.wrapped.wait_closed()

    def get_extra_info(self, name, default=None):
        return self.wrapped.get_extra_info(name, default)


@unittest.skipUnless(IPV6_LOOPBACK, "IPv6 loopback is unavailable on this runner")
class CryptoSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server_identity = Identity.generate()
        self.client_identity = Identity.generate()
        self.received: list[tuple[int, bytes]] = []
        self.server_ready = asyncio.Event()
        self.server_error: BaseException | None = None

        async def handle(reader, writer):
            try:
                session = await server_handshake(reader, writer, self.server_identity)
                kind, body = await session.receive_payload()
                self.received.append((kind, body))
                await session.send_control({"type": "ack", "ok": True})
                self.server_ready.set()
                await asyncio.sleep(0.05)
                await session.close()
            except BaseException as exc:  # noqa: BLE001
                self.server_error = exc
                self.server_ready.set()
            finally:
                writer.close()

        self.server = await asyncio.start_server(handle, "::1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def test_encrypted_frames_do_not_expose_plaintext(self):
        reader, base_writer = await asyncio.open_connection("::1", self.port)
        recording = RecordingWriter(base_writer)
        client = await client_handshake(
            reader,
            recording,
            self.client_identity,
            expected_peer_id=self.server_identity.peer_id,
        )
        self.assertEqual(client.peer_id, self.server_identity.peer_id)
        marker = "TOP_SECRET_MARKER_9137_绝密"
        await client.send_control({"type": "secret", "text": marker, "name": "Alice"})
        ack = await client.receive_control()
        self.assertEqual(ack["type"], "ack")
        await self.server_ready.wait()
        await client.close()
        if self.server_error:
            raise self.server_error
        self.assertTrue(self.received)
        captured = bytes(recording.captured)
        self.assertNotIn(marker.encode("utf-8"), captured)
        self.assertNotIn(b"secret", captured)
        self.assertNotIn(b"Alice", captured)
        # The only clear-text protocol artifact is the fixed magic header.
        self.assertTrue(captured.startswith(b"V6LC"))

    async def test_identity_pinning_rejects_wrong_server(self):
        reader, writer = await asyncio.open_connection("::1", self.port)
        with self.assertRaises(IdentityMismatch):
            await client_handshake(
                reader,
                writer,
                self.client_identity,
                expected_peer_id="00" * 32,
            )
        writer.close()
        await self.server_ready.wait()


class EncryptedVaultTests(unittest.TestCase):
    def test_vault_is_encrypted_and_reloadable(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Vault(directory, display_name="AliceSecretName")
            contact = vault.create_contact(
                "BobConfidential",
                addresses=[{"host": "2001:db8::42", "port": 4455}],
            )
            vault.add_text_message(
                contact["id"],
                "out",
                "TOP_SECRET_CHAT_MESSAGE_7f3a 这是秘密",
                "sent",
            )
            raw = vault.vault_path.read_bytes()
            self.assertTrue(raw.startswith(b"V6VAULT1"))
            self.assertNotIn(b"BobConfidential", raw)
            self.assertNotIn(b"2001:db8::42", raw)
            self.assertNotIn(b"TOP_SECRET_CHAT_MESSAGE_7f3a", raw)
            self.assertNotIn("这是秘密".encode("utf-8"), raw)
            self.assertNotIn(b"AliceSecretName", raw)

            reopened = Vault(directory)
            self.assertEqual(reopened.profile()["display_name"], "AliceSecretName")
            self.assertEqual(reopened.profile()["listen_port"], 4455)
            contacts = reopened.list_contacts()
            self.assertEqual(contacts[0]["name"], "BobConfidential")
            messages = reopened.list_messages(contact["id"])
            self.assertEqual(messages[0]["text"], "TOP_SECRET_CHAT_MESSAGE_7f3a 这是秘密")

    def test_interrupted_file_is_kept_as_paused_on_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Vault(directory, display_name="Resume")
            contact = vault.create_contact("Peer")
            attachments = Path(directory) / "attachments"
            attachments.mkdir(parents=True, exist_ok=True)
            partial_path = attachments / "partial.v6f"
            partial_path.write_bytes(b"V6FILE01partial")
            vault.add_file_message(
                contact["id"],
                "in",
                {
                    "name": "movie.mp4",
                    "size": 4096,
                    "chunk_size": 1024,
                    "storage": "encrypted",
                    "enc_path": "attachments/partial.v6f",
                    "key": "AA==",
                },
                "receiving",
                origin_id="a" * 32,
            )
            reopened = Vault(directory)
            messages = reopened.list_messages(contact["id"])
            self.assertEqual(messages[0]["status"], "paused")
            self.assertIn("继续", messages[0]["error"])
            self.assertTrue(partial_path.exists(), "partial container must survive recovery")

    def test_incoming_filename_is_sanitized(self):
        self.assertEqual(sanitize_filename("../../evil.txt"), "evil.txt")
        self.assertEqual(sanitize_filename("a:b?.zip"), "a_b_.zip")

    def test_endpoint_parsing(self):
        self.assertEqual(parse_endpoint("2001:db8::1"), ("2001:db8::1", 4455))
        self.assertEqual(parse_endpoint("[2001:db8::1]:9000"), ("2001:db8::1", 9000))
        self.assertEqual(parse_endpoint("[fe80::1%12]:9000"), ("fe80::1%12", 9000))
        with self.assertRaises(ValueError):
            parse_endpoint("127.0.0.1:4455")
        # An unbracketed IPv6 literal is treated as a host, not host:port.
        self.assertEqual(parse_endpoint("::1:4455"), ("::1:4455", 4455))


class ShareCardTests(unittest.TestCase):
    def test_card_round_trip_parser(self):
        peer_id = Identity.generate().peer_id
        card = "\n".join(
            [
                "V6Link 加密名片",
                "昵称: 林晚",
                f"身份指纹: {peer_id}",
                "IPv6 地址:",
                "  [240e:3b0:7a21:5c00::8a3]:4455  (全局)",
                "  [fe80::1%eth0]:4455  (链路本地)",
                "  [::1]:4455  (本机回环)",
                "监听端口: 4455",
            ]
        )
        parsed = parse_share_card(card)
        self.assertEqual(parsed["name"], "林晚")
        self.assertEqual(parsed["peer_id"], peer_id)
        self.assertEqual(parsed["addresses"][0]["host"], "240e:3b0:7a21:5c00::8a3")
        self.assertEqual(parsed["addresses"][0]["port"], 4455)
        self.assertEqual(len(parsed["addresses"]), 1, "loopback and link-local must be ignored")
        with self.assertRaises(ValueError):
            parse_share_card("随便一段文字")
        with self.assertRaises(ValueError):
            parse_share_card(
                "V6Link 加密名片\n昵称: 本机\n身份指纹: "
                + peer_id
                + "\nIPv6 地址:\n  [::1]:4455\n  [fe80::1%eth0]:4455"
            )

    def test_address_discovery_dedupes_and_hides_local_only_addresses(self):
        route = "2001:db8::10"
        candidates = [
            LocalAddress(host=route, interface="lo", kind="global"),
            LocalAddress(host="2001:db8::20", interface="eth0", kind="global"),
            LocalAddress(host="fe80::1%eth0", interface="eth0", kind="link-local"),
            LocalAddress(host="::1", interface="loopback", kind="loopback"),
        ]
        with patch("v6link.netutil._default_route_address", return_value=route), patch(
            "v6link.netutil._candidate_psutil_addresses", return_value=candidates
        ):
            all_addresses = local_ipv6_addresses()
            shared = shareable_ipv6_addresses()
        hosts = [entry.host for entry in all_addresses]
        self.assertEqual(hosts.count(route), 1)
        self.assertIn("::1", hosts)
        self.assertIn("fe80::1%eth0", hosts)
        self.assertEqual([entry.host for entry in shared], [route, "2001:db8::20"])


class AttachmentEncryptionTests(unittest.TestCase):
    def test_parallel_chunk_container(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = (b"ATTACHMENT_SECRET_MARKER_4a19" + os.urandom(8192)) * 19
            size = len(payload)
            writer = AttachmentWriter.create(directory, size, chunk_size=64 * 1024)
            chunks = [
                payload[offset : offset + 64 * 1024]
                for offset in range(0, size, 64 * 1024)
            ]
            for index in reversed(range(len(chunks))):
                writer.write_chunk(index, chunks[index])
            meta = writer.metadata("../../secret-name.txt")
            writer.finish()

            container = (Path(directory) / meta["enc_path"]).read_bytes()
            self.assertNotIn(b"ATTACHMENT_SECRET_MARKER_4a19", container)
            self.assertNotIn(b"secret-name", container)
            self.assertTrue(container.startswith(b"V6FILE01"))
            decrypted = b"".join(iter_plaintext(directory, meta))
            self.assertEqual(decrypted, payload)
            self.assertEqual(meta["name"], "secret-name.txt")

            # GCM must detect any modified ciphertext byte.
            tampered = bytearray(container)
            tampered[HEADER_SIZE + 7] ^= 0x80
            (Path(directory) / meta["enc_path"]).write_bytes(tampered)
            with self.assertRaises(VaultError):
                b"".join(iter_plaintext(directory, meta))

    def test_partial_container_can_resume_with_verified_bitmap(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = (b"RESUME_SECRET_MARKER_23c1" + os.urandom(8192)) * 40
            chunk_size = 64 * 1024
            chunks = [
                payload[offset : offset + chunk_size]
                for offset in range(0, len(payload), chunk_size)
            ]
            writer = AttachmentWriter.create(directory, len(payload), chunk_size=chunk_size)
            writer.write_chunk(0, chunks[0])
            writer.write_chunk(2, chunks[2])
            meta = writer.metadata("resume.bin")
            writer.suspend()

            container = Path(directory) / meta["enc_path"]
            self.assertTrue(container.exists())
            reopened = AttachmentWriter.open_existing(directory, meta)
            self.assertEqual(reopened.received_chunks, 2)
            bitmap = reopened.received_bitmap()
            self.assertEqual(bitmap[0] & 0b00000101, 0b00000101)
            for index in range(len(chunks)):
                if index not in (0, 2):
                    reopened.write_chunk(index, chunks[index])
            reopened.finish()
            decrypted = b"".join(iter_plaintext(directory, meta))
            self.assertEqual(decrypted, payload)

    def test_empty_file(self):
        with tempfile.TemporaryDirectory() as directory:
            writer = AttachmentWriter.create(directory, 0, chunk_size=64 * 1024)
            meta = writer.metadata("empty.txt")
            writer.finish()
            self.assertEqual(list(iter_plaintext(directory, meta)), [])





