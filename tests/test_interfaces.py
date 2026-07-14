from henry.interfaces import Integration, Memory, Sandbox, ToolsetProvider
from henry.testing import FakeIntegration, FakeMemory, FakeSandbox


def test_fakes_satisfy_protocols() -> None:
    assert isinstance(FakeMemory(), Memory)
    assert isinstance(FakeSandbox(), Sandbox)
    assert isinstance(FakeIntegration(), Integration)


class _WithToolset:
    def toolset(self):
        return object()


def test_toolset_provider_is_structural_and_narrow() -> None:
    assert isinstance(_WithToolset(), ToolsetProvider)
    assert not isinstance(FakeIntegration(), ToolsetProvider)
