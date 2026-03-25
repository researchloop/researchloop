#!/bin/bash
# Don't use set -e — we want sshd to start even if SGE setup partially fails.

# Create a non-root user for SGE job submission FIRST.
id sgeuser >/dev/null 2>&1 || useradd -m -s /bin/bash sgeuser

# Install SSH public key if mounted.
if [ -f /tmp/test_key.pub ]; then
    for homedir in /root /home/sgeuser; do
        mkdir -p "$homedir/.ssh"
        cp /tmp/test_key.pub "$homedir/.ssh/authorized_keys"
        chmod 700 "$homedir/.ssh"
        chmod 600 "$homedir/.ssh/authorized_keys"
    done
    chown -R sgeuser:sgeuser /home/sgeuser/.ssh
fi

# Generate SSH host keys if missing.
ssh-keygen -A 2>/dev/null || true

# Run the original SGE boot script. It does:
# 1. inst_sge (install SGE master + exec)
# 2. qconf -as (add submit host)
# 3. Allow root to submit (min_uid=0)
# Pass "true" so it runs exec true instead of exec bash.
echo "Starting SGE installation..."
/root/boot-sge.sh true 2>&1 || echo "WARNING: boot-sge.sh exited with $?"

# Source SGE settings if the install succeeded.
if [ -f /etc/profile.d/sge_settings.sh ]; then
    . /etc/profile.d/sge_settings.sh
    echo "SGE ready. SGE_ROOT=$SGE_ROOT"

    # Make SGE commands available via SSH sessions.
    echo "PermitUserEnvironment yes" >> /etc/ssh/sshd_config

    # Add SGE to system-wide environment and sgeuser's bashrc.
    echo "PATH=$PATH" > /etc/environment
    echo ". /etc/profile.d/sge_settings.sh 2>/dev/null || true" >> /home/sgeuser/.bashrc
    echo ". /etc/profile.d/sge_settings.sh 2>/dev/null || true" >> /root/.bashrc

    # Also create symlinks so SGE commands are on default PATH.
    SGE_BIN="$SGE_ROOT/bin/$($SGE_ROOT/util/arch 2>/dev/null || echo lx-amd64)"
    if [ -d "$SGE_BIN" ]; then
        for cmd in qsub qstat qdel qconf qacct; do
            [ -f "$SGE_BIN/$cmd" ] && ln -sf "$SGE_BIN/$cmd" /usr/local/bin/$cmd
        done
    fi

    for homedir in /root /home/sgeuser; do
        mkdir -p "$homedir/.ssh"
        cat > "$homedir/.ssh/environment" << ENVEOF
SGE_ROOT=$SGE_ROOT
SGE_CELL=${SGE_CELL:-default}
SGE_CLUSTER_NAME=${SGE_CLUSTER_NAME:-}
SGE_QMASTER_PORT=${SGE_QMASTER_PORT:-}
SGE_EXECD_PORT=${SGE_EXECD_PORT:-}
PATH=$PATH
ENVEOF
        chmod 600 "$homedir/.ssh/environment"
    done
    chown -R sgeuser:sgeuser /home/sgeuser 2>/dev/null || true
else
    echo "WARNING: SGE settings not found, SGE may not be available"
fi

# Give sgeuser write access to the test directory.
mkdir -p /tmp/researchloop
chown sgeuser:sgeuser /tmp/researchloop

echo "Starting sshd..."
exec /usr/sbin/sshd -D -e
