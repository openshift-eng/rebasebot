FROM registry.access.redhat.com/ubi8/python-39

WORKDIR /src
COPY . .
RUN python -m pip install .

ENTRYPOINT [ "/opt/app-root/bin/merge-bot" ]
