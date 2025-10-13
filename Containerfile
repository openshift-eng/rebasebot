FROM registry.access.redhat.com/ubi9/python-312 AS base

USER root

ARG TARGETARCH=amd64
ARG GO_VERSION=1.24.3

# The Go project uses "arm64" instead of "aarch64" in filenames
RUN dnf install -y tar gzip gpgme gpgme-devel pkgconfig && \
    ARCH=$(case "${TARGETARCH:-amd64}" in aarch64) echo "arm64" ;; amd64) echo "amd64" ;; *) echo "${TARGETARCH:-amd64}" ;; esac) && \
    curl -Ls "https://go.dev/dl/go${GO_VERSION}.linux-${ARCH}.tar.gz" | tar -C /usr/local -xz && \
    python -m pip install --no-cache-dir uv && \
    dnf clean all && rm -rf /var/cache/dnf /tmp/*

ENV PATH="/usr/local/go/bin:$PATH"

WORKDIR /src
COPY . .
RUN python -m pip install .

WORKDIR /working
RUN rm -rf /src
RUN chown default .
RUN chmod 0777 .

USER default
ENTRYPOINT [ "/opt/app-root/bin/rebasebot" ]
