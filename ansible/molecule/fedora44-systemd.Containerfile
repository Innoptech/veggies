# Systemd-enabled Fedora 44 image for Molecule scenarios (built by prepare.yml).
# Kept in-repo so the test supply chain is boring: upstream base + dnf, nothing else.
FROM registry.fedoraproject.org/fedora:44

RUN dnf -y install systemd python3 python3-libselinux sudo iproute \
    && dnf clean all

ENV container=podman
STOPSIGNAL SIGRTMIN+3
CMD ["/usr/sbin/init"]
