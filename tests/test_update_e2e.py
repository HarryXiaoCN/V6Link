"""End-to-end tests for peer-to-peer application updates."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import socket
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

from v6link.crypto import EncryptedSession, Identity
from v6link.engine import ChatEngine
from v6link.errors import UpdateError
from v6link.updates import (
    build_manifest,
    compare_versions,
    is_newer,
    normalize_manifest,
    normalize_version,
    pending_dir,
    prepare_install,
    verify_manifest,
)
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


class EventCollector:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._new = asyncio.Event()
        self._consumed: set[int] = set()

    def __call__(self, payload: dict[str, Any]) -> None:
        self.events.append(payload)
        self._new.set()

    async def wait(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for index, event in enumerate(self.events):
                if index in self._consumed:
                    continue
                if predicate(event):
                    self._consumed.add(index)
                    return event
            remaining = deadline - loop.time()
            if remaining <= 0:
                recent = [event.get("type") for event in self.events[-15:]]
                raise AssertionError(f"等待事件超时；最近事件：{recent}")
            self._new.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._new.wait(), timeout=remaining)


class UpdateUnitTests(unittest.TestCase):
    def test_version_comparison_is_numeric_and_zero_padded(self):
        self.assertEqual(compare_versions("0.3", "0.3.0"), 0)
        self.assertEqual(compare_versions("0.3.1", "0.3.0"), 1)
        self.assertEqual(compare_versions("0.9.9", "0.10.0"), -1)
        self.assertTrue(is_newer("1.0", "0.99.99"))
        self.assertFalse(is_newer("0.2.2", "0.2.2"))
        self.assertEqual(normalize_version(" 0.3.0 "), "0.3.0")
        with self.assertRaises(UpdateError):
            normalize_version("v0.3.0-beta")

    def test_manifest_signature_binds_payload_and_identity(self):
        with tempfile.TemporaryDirectory(prefix="v6link-update-unit-") as folder:
            payload = Path(folder) / "V6Link-0.4.0-source.zip"
            payload.write_bytes(os.urandom(64 * 1024))
            signer = Identity.generate()
            other = Identity.generate()
            manifest = build_manifest(
                signer,
                path=payload,
                kind="source",
                filename=payload.name,
                notes="测试更新",
                version="0.4.0",
            )
            cleaned = normalize_manifest(manifest)
            verify_manifest(cleaned, signer.public_bytes)
            self.assertEqual(cleaned["version"], "0.4.0")
            self.assertEqual(cleaned["notes"], "测试更新")
            self.assertEqual(cleaned["size"], payload.stat().st_size)

            tampered = dict(cleaned)
            tampered["sha256"] = "f" * 64
            with self.assertRaises(UpdateError):
                verify_manifest(tampered, signer.public_bytes)

            with self.assertRaises(UpdateError):
                verify_manifest(cleaned, other.public_bytes)

    def test_manifest_rejects_unexpected_fields(self):
        base = {
            "offer_id": "a" * 32,
            "version": "0.4.0",
            "size": 1024 * 1024,
            "sha256": "b" * 64,
            "kind": "exe",
            "filename": "V6Link-0.4.0.exe",
            "signature": "A" * 86 + "==",
        }
        self.assertEqual(normalize_manifest(base)["kind"], "exe")
        for field, value in (
            ("offer_id", "../../etc/passwd"),
            ("version", "next"),
            ("size", 12),
            ("size", 1024 ** 3),
            ("sha256", "short"),
            ("kind", "dll"),
            ("filename", "../evil.exe"),
        ):
            broken = dict(base)
            broken[field] = value
            with self.assertRaises(UpdateError, msg=field):
                normalize_manifest(broken)


@unittest.skipUnless(IPV6_LOOPBACK, "IPv6 loopback is unavailable on this runner")
class UpdateEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._dirs: list[tempfile.TemporaryDirectory] = []
        self._engines: list[ChatEngine] = []
        self._files: list[Path] = []

    async def asyncTearDown(self) -> None:
        for engine in reversed(self._engines):
            with contextlib.suppress(Exception):
                await engine.stop()
        for path in self._files:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
        for directory in self._dirs:
            directory.cleanup()

    async def _new_engine(self, name: str, version: str):
        directory = tempfile.TemporaryDirectory(prefix=f"v6link-update-{name}-")
        self._dirs.append(directory)
        vault = Vault(directory.name, display_name=name, listen_port=0)
        events = EventCollector()
        engine = ChatEngine(
            vault,
            events,
            listen_port=0,
            downloads_dir=Path(directory.name) / "downloads",
            version=version,
        )
        await engine.start()
        self._engines.append(engine)
        return engine, events

    async def _new_pair(self, newer: str = "0.4.0", older: str = "0.3.0"):
        alice, alice_events = await self._new_engine("Alice", newer)
        bob, bob_events = await self._new_engine("Bob", older)
        alice_contact = await alice.add_contact(
            "Bob",
            peer_id=bob.identity.peer_id,
            addresses=[{"host": "::1", "port": bob.port}],
        )
        bob_contact = await bob.add_contact(
            "Alice",
            peer_id=alice.identity.peer_id,
            addresses=[{"host": "::1", "port": alice.port}],
        )
        return alice, alice_events, alice_contact, bob, bob_events, bob_contact

    def _make_payload(self, size: int = 8 * 1024 * 1024) -> Path:
        handle, name = tempfile.mkstemp(prefix="v6link-update-payload-", suffix=".zip")
        with os.fdopen(handle, "wb") as stream:
            stream.write(os.urandom(size))
        source = Path(name)
        self._files.append(source)
        return source

    async def test_newer_peer_pushes_update_and_receiver_stages_it(self):
        alice, alice_events, alice_contact, bob, bob_events, _bob_contact = (
            await self._new_pair()
        )
        payload = self._make_payload()
        expected = hashlib.sha256(payload.read_bytes()).hexdigest()
        filename = "V6Link-0.4.0-source.zip"

        def fake_payload(data_dir, *, version=None):
            return payload, "source", filename

        with patch("v6link.engine.updates.payload_for_offer", fake_payload):
            await alice.push_update(alice_contact["id"], notes="这次更新了自动更新")

        offer = await bob_events.wait(
            lambda event: event.get("type") == "update_offer_received",
            timeout=30,
        )
        info = offer["offer"]
        self.assertEqual(info["version"], "0.4.0")
        self.assertEqual(info["current_version"], "0.3.0")
        self.assertEqual(info["size"], payload.stat().st_size)
        self.assertEqual(info["sha256"], expected)
        self.assertEqual(info["notes"], "这次更新了自动更新")
        self.assertEqual(info["contact_name"], "Alice")

        await bob.respond_update_offer(info["offer_id"], True)
        downloaded = await bob_events.wait(
            lambda event: event.get("type") == "update_downloaded",
            timeout=60,
        )
        staged = Path(downloaded["path"])
        self.assertEqual(staged.parent.resolve(), pending_dir(bob.vault.data_dir).resolve())
        self.assertEqual(staged.name, filename)
        self.assertEqual(staged.stat().st_size, payload.stat().st_size)
        self.assertEqual(hashlib.sha256(staged.read_bytes()).hexdigest(), expected)
        self.assertFalse(downloaded["installable"])

        progress = [
            event for event in bob_events.events if event.get("type") == "update_progress"
        ]
        self.assertTrue(progress)
        self.assertEqual(progress[-1]["total"], payload.stat().st_size)
        self.assertEqual(progress[-1]["direction"], "receive")

        pushed = await alice_events.wait(
            lambda event: event.get("type") == "update_pushed",
            timeout=30,
        )
        self.assertEqual(pushed["size"], payload.stat().st_size)
        self.assertEqual(pushed["version"], "0.4.0")

        leftovers = list(
            (bob.vault.data_dir / "updates" / "incoming").glob("*.part")
        )
        self.assertEqual(leftovers, [])

    async def test_receiver_can_decline_the_offer(self):
        alice, alice_events, alice_contact, bob, bob_events, _bob_contact = (
            await self._new_pair()
        )
        payload = self._make_payload(4096)
        with patch(
            "v6link.engine.updates.payload_for_offer",
            lambda data_dir, *, version=None: (payload, "source", "V6Link-0.4.0-source.zip"),
        ):
            await alice.push_update(alice_contact["id"])
        offer = await bob_events.wait(
            lambda event: event.get("type") == "update_offer_received",
            timeout=30,
        )
        await bob.respond_update_offer(offer["offer"]["offer_id"], False)
        declined = await alice_events.wait(
            lambda event: event.get("type") == "update_declined",
            timeout=30,
        )
        self.assertEqual(declined["version"], "0.4.0")
        pending = list((bob.vault.data_dir / "updates" / "pending").glob("*"))
        self.assertEqual(pending, [])

    async def test_push_is_refused_when_receiver_is_not_older(self):
        alice, _alice_events, alice_contact, _bob, _bob_events, _bob_contact = (
            await self._new_pair(newer="0.3.0", older="0.4.0")
        )
        payload = self._make_payload(4096)
        with patch(
            "v6link.engine.updates.payload_for_offer",
            lambda data_dir, *, version=None: (payload, "source", "V6Link-0.3.0-source.zip"),
        ), self.assertRaises(UpdateError):
            await alice.push_update(alice_contact["id"])

    async def test_cancel_during_pull_removes_partial_download(self):
        alice, _alice_events, alice_contact, bob, bob_events, _bob_contact = (
            await self._new_pair()
        )
        payload = self._make_payload(16 * 1024 * 1024)
        with patch(
            "v6link.engine.updates.payload_for_offer",
            lambda data_dir, *, version=None: (payload, "source", "V6Link-0.4.0-source.zip"),
        ):
            await alice.push_update(alice_contact["id"])
            offer = await bob_events.wait(
                lambda event: event.get("type") == "update_offer_received",
                timeout=30,
            )
            original_chunk = EncryptedSession.send_file_chunk

            async def slow_chunk(session, offset, data):
                await asyncio.sleep(0.05)
                return await original_chunk(session, offset, data)

            with patch.object(EncryptedSession, "send_file_chunk", slow_chunk):
                await bob.respond_update_offer(offer["offer"]["offer_id"], True)
                await bob_events.wait(
                    lambda event: event.get("type") == "update_progress"
                    and int(event.get("transferred", 0) or 0) > 0,
                    timeout=30,
                )
                await bob.cancel_update(offer["offer"]["offer_id"])

        canceled = await bob_events.wait(
            lambda event: event.get("type") == "update_canceled",
            timeout=20,
        )
        self.assertEqual(canceled["offer_id"], offer["offer"]["offer_id"])
        await asyncio.sleep(0.2)
        self.assertFalse(
            any(event.get("type") == "update_downloaded" for event in bob_events.events)
        )
        parts = list((bob.vault.data_dir / "updates" / "incoming").glob("*.part"))
        self.assertEqual(parts, [])


class InstallPlanTests(unittest.TestCase):
    """The swap script is generated, never executed, by these tests."""

    def _layout(self, folder: Path) -> tuple[Path, Path]:
        data_dir = folder / "data"
        pending = pending_dir(data_dir)
        pending.mkdir(parents=True)
        target = folder / "app's dir" / "V6Link.exe"
        target.parent.mkdir()
        target.write_bytes(b"MZ" + os.urandom(8 * 1024))
        staged = pending / "V6Link-0.4.0.exe"
        staged.write_bytes(b"MZ" + os.urandom(8 * 1024))
        return target, staged

    def test_install_plan_renders_quoted_swap_script(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory(prefix="v6link-install-") as folder:
            root = Path(folder)
            target, staged = self._layout(root)
            digest = hashlib.sha256(staged.read_bytes()).hexdigest()
            with (
                patch("v6link.updates.running_executable", lambda: target),
                patch("v6link.updates.sys", SimpleNamespace(platform="win32")),
            ):
                plan = prepare_install(
                    root / "data",
                    staged,
                    version="0.4.0",
                    sha256=digest,
                    size=staged.stat().st_size,
                )
            text = plan.script.read_text(encoding="utf-8-sig")
            self.assertIn("Move-Item -LiteralPath $target -Destination $backup -Force", text)
            self.assertIn("Copy-Item -LiteralPath $staged -Destination $target -Force", text)
            self.assertIn("V6Link.exe.old-", plan.backup.name)
            self.assertIn("app''s dir", text)
            self.assertIn("Get-Process -Id $oldProcessId", text)
            self.assertIn(str(plan.staged), text)
            self.assertTrue(plan.script.name.endswith(".ps1"))

    def test_install_rejects_tampered_or_foreign_staged_files(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory(prefix="v6link-install-") as folder:
            root = Path(folder)
            target, staged = self._layout(root)
            digest = hashlib.sha256(staged.read_bytes()).hexdigest()
            with (
                patch("v6link.updates.running_executable", lambda: target),
                patch("v6link.updates.sys", SimpleNamespace(platform="win32")),
            ):
                with self.assertRaises(UpdateError):
                    prepare_install(
                        root / "data",
                        staged,
                        version="0.4.0",
                        sha256="0" * 64,
                        size=staged.stat().st_size,
                    )
                with self.assertRaises(UpdateError):
                    prepare_install(
                        root / "data",
                        staged,
                        version="0.4.0",
                        sha256=digest,
                        size=staged.stat().st_size + 1,
                    )
                outside = root / "outside.exe"
                outside.write_bytes(b"MZ" + os.urandom(8 * 1024))
                with self.assertRaises(UpdateError):
                    prepare_install(
                        root / "data",
                        outside,
                        version="0.4.0",
                        sha256=hashlib.sha256(outside.read_bytes()).hexdigest(),
                        size=outside.stat().st_size,
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
