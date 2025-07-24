FROM registry.access.redhat.com/ubi9/python-312

USER root

ENV GO_VERSION=1.24.3
ENV PATH="/usr/local/go/bin:$PATH"

RUN dnf install -y tar gzip git make && \
    curl -Ls https://golang.org/dl/go${GO_VERSION}.linux-amd64.tar.gz | \
    tar -C /usr/local -zxvf - go/

WORKDIR /src
COPY . .
RUN python -m pip install .

WORKDIR /working
RUN rm -rf /src
RUN chown 1001:0 .
RUN chmod 0777 .

USER 1001
ENTRYPOINT [ "/opt/app-root/bin/rebasebot" ]
