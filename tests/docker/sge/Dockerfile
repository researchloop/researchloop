FROM kgutwin/simple-sge

# Install SSH server and Python 3 (needed for job scripts).
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        openssh-server python3 curl && \
    rm -rf /var/lib/apt/lists/* && \
    mkdir -p /run/sshd

# Configure sshd: allow root login, key auth, high MaxAuthTries.
RUN sed -i 's/#PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config && \
    sed -i 's/#PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config && \
    sed -i 's/#MaxAuthTries.*/MaxAuthTries 20/' /etc/ssh/sshd_config

# Force SGE's install to run as root. The default inst_template.conf sets
# ADMIN_USER=sgeadmin, which makes inst_sge route through the non-setuid
# `adminrun` helper to drop privileges. On the GitHub Actions runner that
# drop-privileges path fails ("cannot create /tmp/configuration_*:
# Permission denied") so inst_sge bails out before qmaster/execd install,
# leaving the container without qconf/qsub. Running the install as root
# avoids adminrun entirely and works on both local Docker and CI.
RUN sed -i 's/^ADMIN_USER=.*/ADMIN_USER=root/' \
    /opt/sge/util/install_modules/inst_template.conf

# Install mock claude CLI.
COPY mock_claude.sh /usr/local/bin/claude
RUN chmod +x /usr/local/bin/claude

# Copy entrypoint that starts SGE + sshd.
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 22

ENTRYPOINT ["/entrypoint.sh"]
