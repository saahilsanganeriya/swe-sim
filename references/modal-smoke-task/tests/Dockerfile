FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends python3 curl ca-certificates && rm -rf /var/lib/apt/lists/*
COPY test.sh /tests/test.sh
RUN chmod +x /tests/test.sh
