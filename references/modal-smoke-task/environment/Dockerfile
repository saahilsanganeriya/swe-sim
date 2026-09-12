FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends curl bash build-essential git ca-certificates python3 && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1001 agent
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh && uv --version
RUN mkdir -p /app/.git /app/.venv/lib && echo gitjunk > /app/.git/HEAD && head -c 20000 /dev/urandom > /app/.venv/lib/big.bin \
 && mkdir -p /app/swesim-receipt && echo "receipt: nodot" > /app/swesim-receipt/receipt \
 && chown -R agent:agent /app && chown -R root:root /app/swesim-receipt
USER agent
ENV HOME=/home/agent
RUN uv python install 3.12 && uv tool install --python 3.12 mini-swe-agent --with litellm --with orjson --with fastapi \
 && /home/agent/.local/bin/mini-swe-agent --help >/dev/null && echo 'export PATH="$HOME/.local/bin:$PATH"' >> /home/agent/.bashrc
USER root
RUN chown -R agent:agent /home/agent && chmod -R u+rwX /home/agent && ls -la /home/agent/.local/bin | head -5
WORKDIR /app
