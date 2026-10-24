"""Shared exception types with user-presentable messages."""


class V6LinkError(Exception):
    """Base class for expected application errors."""


class VaultError(V6LinkError):
    """The encrypted local vault could not be created, read, or written."""


class HandshakeError(V6LinkError):
    """An encrypted peer handshake failed."""


class IdentityMismatch(HandshakeError):
    """A peer presented a different identity than the pinned contact identity."""


class PeerUnreachable(V6LinkError):
    """None of a contact's saved IPv6 endpoints could be reached."""

    def __init__(self, message: str, attempts: int = 0):
        super().__init__(message)
        self.attempts = attempts


class PeerApprovalRequired(V6LinkError):
    """The remote peer has not accepted this device as a friend yet."""

    def __init__(self, peer_id: str, message: str = "对方尚未接受你的好友请求"):
        super().__init__(message)
        self.peer_id = peer_id


class TransferError(V6LinkError):
    """A file transfer failed."""


class UpdateError(V6LinkError):
    """An application update offer, download or install was rejected."""
