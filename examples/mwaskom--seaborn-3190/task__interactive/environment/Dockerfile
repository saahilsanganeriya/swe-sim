FROM swebench/sweb.eval.x86_64.mwaskom_1776_seaborn-3190@sha256:5ce876a52439385a2638f21617febf25a333c24b0db555cb9e37230b4db2cdcc
RUN cd /testbed && git reset --hard 4a9e54962a29c12a8b103d75f838e0e795a6974d
USER root
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends curl bash git ca-certificates procps && rm -rf /var/lib/apt/lists/*
RUN if id agent >/dev/null 2>&1; then echo 'agent user pre-exists in base image; rejected for this pilot' >&2; exit 1; fi && useradd -m agent
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh && uv --version
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
RUN uv python install 3.12 && ln -s "$(uv python find 3.12)" /usr/local/bin/swesim-python && /usr/local/bin/swesim-python -I -c 'import sys; print(sys.version)'
# Assert the prepared checkout, apply the submodule rule (declared paths empty, placeholders removed), then record the
# base file manifest (the verifier image runs the same steps and records the same one).
RUN cd /testbed && test "$(git rev-parse HEAD)" = 4a9e54962a29c12a8b103d75f838e0e795a6974d \
 && if [ -e .gitmodules ]; then git config -f .gitmodules -l >/dev/null 2>&1 || { echo 'malformed .gitmodules rejected' >&2; exit 1; }; git config -f .gitmodules --get-regexp '^submodule\..*\.path$' | awk 'NF>2{bad=1} END{exit bad}' || { echo 'submodule path with whitespace rejected' >&2; exit 1; }; for p in $(git config -f .gitmodules --get-regexp '^submodule\..*\.path$' | awk '{print $2}'); do if [ -L "$p" ] || { [ -e "$p" ] && { [ ! -d "$p" ] || [ -n "$(ls -A "$p")" ]; }; }; then echo "populated submodule path rejected: $p" >&2; exit 1; fi; done; git submodule deinit --all -f >/dev/null 2>&1 || true; for p in $(git config -f .gitmodules --get-regexp '^submodule\..*\.path$' | awk '{print $2}'); do { [ -d "$p" ] && rmdir "$p"; } || true; done; fi \
 && mkdir -p /opt/swesim && find . -path ./.git -prune -o -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > /opt/swesim/base-manifest.txt
# A new base-only git object database removes remote refs, future objects and reflogs.
RUN cd /testbed && rm -rf .git && git init -q && git config user.name swesim && git config user.email swesim@localhost \
 && git add -A && git -c core.hooksPath=/dev/null commit -qm 'scenario base' && git reflog expire --expire=now --all
RUN mkdir -p /opt/swesim/bin /var/swesim-collect && chmod 700 /var/swesim-collect
COPY collect.py /opt/swesim/collect.py
COPY shims/ask-user shims/request-review /opt/swesim/bin/
RUN chmod 755 /opt/swesim/collect.py /opt/swesim/bin/* && ln -s /opt/swesim/bin/ask-user /usr/local/bin/ask-user && ln -s /opt/swesim/bin/request-review /usr/local/bin/request-review
RUN chown -R agent:agent /testbed /home/agent
USER agent
ENV HOME=/home/agent
ENV UV_PYTHON_INSTALL_DIR=/home/agent/.local/share/uv/python
RUN uv python install 3.12 && uv tool install --python 3.12 'mini-swe-agent==2.4.6' --with litellm --with orjson --with fastapi \
 && /home/agent/.local/bin/mini-swe-agent --help >/dev/null && echo 'export PATH="$HOME/.local/bin:$PATH"' >> /home/agent/.bashrc
USER root
RUN chown -R agent:agent /home/agent && chmod -R u+rwX /home/agent
WORKDIR /testbed
