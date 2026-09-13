FROM swebench/sweb.eval.x86_64.mwaskom_1776_seaborn-3190@sha256:5ce876a52439385a2638f21617febf25a333c24b0db555cb9e37230b4db2cdcc
RUN cd /testbed && git reset --hard 4a9e54962a29c12a8b103d75f838e0e795a6974d
USER root
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends curl git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN if id tester >/dev/null 2>&1; then echo 'tester user pre-exists in base image; rejected for this pilot' >&2; exit 1; fi && useradd -m tester
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh && uv --version
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
RUN uv python install 3.12 && ln -s "$(uv python find 3.12)" /usr/local/bin/swesim-python && /usr/local/bin/swesim-python -I -c 'import sys; print(sys.version)'
# Assert the prepared checkout; declared submodule paths must be empty (deinit'ed and removed so paths match the agent image);
# the pilot rejects populated submodules, symlinks and LFS pointer fixtures; record the same base manifest as the agent image.
RUN cd /testbed && test "$(git rev-parse HEAD)" = 4a9e54962a29c12a8b103d75f838e0e795a6974d \
 && if [ -e .gitmodules ]; then git config -f .gitmodules -l >/dev/null 2>&1 || { echo 'malformed .gitmodules rejected' >&2; exit 1; }; git config -f .gitmodules --get-regexp '^submodule\..*\.path$' | awk 'NF>2{bad=1} END{exit bad}' || { echo 'submodule path with whitespace rejected' >&2; exit 1; }; for p in $(git config -f .gitmodules --get-regexp '^submodule\..*\.path$' | awk '{print $2}'); do if [ -L "$p" ] || { [ -e "$p" ] && { [ ! -d "$p" ] || [ -n "$(ls -A "$p")" ]; }; }; then echo "populated submodule path rejected: $p" >&2; exit 1; fi; done; git submodule deinit --all -f >/dev/null 2>&1 || true; for p in $(git config -f .gitmodules --get-regexp '^submodule\..*\.path$' | awk '{print $2}'); do { [ -d "$p" ] && rmdir "$p"; } || true; done; fi \
 && test -z "$(find . \( -path ./.git -o -path ./.venv -o -name node_modules -o -name __pycache__ -o -name .pytest_cache \) -prune -o -type l -print | head -1)" \
 && test -z "$(grep -rlI --exclude-dir=.git --exclude-dir=.venv 'version https://git-lfs.github.com/spec' . | head -1)" \
 && mkdir -p /opt/swesim && find . -path ./.git -prune -o -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > /opt/swesim/base-manifest.txt
# The untouched base the grader diffs against. Installed build products stay in place under /testbed.
RUN /usr/local/bin/swesim-python -I -c 'import shutil; shutil.copytree("/testbed", "/pristine", ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", "node_modules"))'
COPY test.patch /tests/test.patch
COPY grader.py policy.py parsers.py run_pytest.py grader_config.json test.sh /tests/
RUN chmod 755 /tests/test.sh && chmod -R a+rX /tests /pristine
