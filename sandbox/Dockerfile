FROM python:3.11.14-slim
RUN pip install --no-cache-dir pytest==8.4.2 && mkdir /seed && chmod 755 /seed
COPY entrypoint.sh /entrypoint.sh
RUN chmod 555 /entrypoint.sh
USER 10001:10001
WORKDIR /workspace
ENTRYPOINT ["/entrypoint.sh"]
