from henry.interfaces import Integration, Memory, Sandbox
from henry.testing import FakeIntegration, FakeMemory, FakeSandbox


def test_fakes_satisfy_protocols() -> None:
    assert isinstance(FakeMemory(), Memory)
    assert isinstance(FakeSandbox(), Sandbox)
    assert isinstance(FakeIntegration(), Integration)
