FROM registry.access.redhat.com/ubi8/python-39

USER root
WORKDIR /src
COPY . .
RUN python -m pip install .

WORKDIR /working
RUN rm -rf /src
RUN chown default .
RUN chmod 0777 .

USER default
ENTRYPOINT [ "/opt/app-root/bin/merge-bot" ]
