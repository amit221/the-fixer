# Iynx — GitHub Contribution Agent — isolated environment for Cursor CLI + gh + git
# Based on https://github.com/cleaton/cli-agent-container

FROM debian:bookworm-slim

ARG BUILD_REV=0

ENV LANG=C.UTF-8 \
    HOME=/home/dev \
    PATH=/home/dev/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN set -eux; \
    echo "BUILD_REV=$BUILD_REV"; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        bash \
        curl \
        tar \
        xz-utils \
        ca-certificates \
        libstdc++6 \
        sudo \
        git \
        jq \
        python3 \
        python3-pip \
        python3-venv \
        gnupg; \
    # Node.js 22 from NodeSource (Debian’s nodejs package is older)
    mkdir -p /etc/apt/keyrings; \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg; \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list > /dev/null; \
    apt-get update; \
    apt-get install -y --no-install-recommends nodejs; \
    # Install GitHub CLI
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg; \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg; \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null; \
    apt-get update; \
    apt-get install -y gh; \
    # Configure passwordless sudo for members of 'sudo' group
    printf '%%sudo ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/010-nopasswd-sudo; \
    chmod 0440 /etc/sudoers.d/010-nopasswd-sudo; \
    # Non-root user: many test stacks (e.g. embedded-postgres) refuse to run as root
    useradd -m -u 1000 -G sudo -s /bin/bash dev; \
    mkdir -p "$HOME" /home/dev/workspace; \
    curl -fsSL https://cursor.com/install | bash; \
    chown -R dev:dev "$HOME"; \
    chmod 0777 /home/dev/workspace; \
    # Clean apt caches (keep curl, git, jq, gh for runtime)
    apt-get purge -y --auto-remove gnupg xz-utils; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /home/dev/workspace

USER dev

# Cursor installer places cursor-agent in $HOME/.local/bin
ENTRYPOINT ["cursor-agent"]
CMD ["--help"]
