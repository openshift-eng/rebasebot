FROM registry.access.redhat.com/ubi8/python-39

USER 0:0
WORKDIR /src
COPY . .
RUN python -m pip install .

WORKDIR /working
RUN rm -rf /src
RUN chown 1001:1001 .

USER 1001:1001
ENTRYPOINT [ "/opt/app-root/bin/merge-bot" ]
