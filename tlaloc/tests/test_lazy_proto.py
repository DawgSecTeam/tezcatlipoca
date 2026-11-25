"""Importing this package must not require a protobuf that cannot load on the interpreter.

The Sliver stubs pull google.protobuf's upb extension, which refuses to import on Python 3.14
("Metaclasses with custom tp_new are not supported"). A Mythic range freezes its payload matrix
on the operator's LOCAL interpreter and never touches the Sliver client, so an eager import
turned a working Mythic deploy into an import crash before any C2 work began (live: scrim-one,
run-49ff5a54). The stubs are imported on first USE instead.
"""

from tlaloc.c2 import sliver

STUBS = ("common_pb2", "clientpb", "sliverpb", "services_pb2")


def test_the_protobuf_stubs_are_proxies_not_modules():
    for name in STUBS:
        value = getattr(sliver, name)
        assert isinstance(value, sliver._LazyProto), f"{name} is imported eagerly"


def test_a_proxy_resolves_its_module_only_when_used():
    proxy = sliver._LazyProto(".mythic", "DEFAULT_TRANSPORTS")
    assert proxy._real is None, "constructing the proxy must not import the module"
    assert callable(proxy.count), "attribute access must reach the real object"
    resolved = proxy._load()
    assert resolved is proxy._real, "the resolved object is reused, not re-imported"
    assert isinstance(resolved, tuple)
