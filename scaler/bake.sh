# Toolchain for a pool server. Runs at first boot when the pool has no baked snapshot yet, and
# once per bake (pool-bake.yml) to make that snapshot, so a scaled server boots in about a
# minute instead of installing all of this. No secrets here.
# The list is what jobs find on a GitHub-hosted Ubuntu runner and would otherwise install with sudo:
# the runner user has no sudo, because one server runs jobs of several repositories within its
# billing hour.
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -yq openjdk-17-jdk-headless openjdk-21-jdk-headless git curl wget jq unzip tar gzip make ca-certificates shellcheck \
  python3 python3-pip python3-venv pipx build-essential default-mysql-client sshpass xvfb zip rsync openssh-client gnupg locales zlib1g-dev
# en_US.UTF-8, as on GitHub-hosted runners: some tests need a collation that differs from C.
locale-gen en_US.UTF-8
# Maven 3.9 as on GitHub-hosted runners; Ubuntu's 3.8.7 is too old for current plugins (spotbugs needs 3.8.9).
MAVEN_VERSION=3.9.11
if [ ! -x "/opt/apache-maven-${MAVEN_VERSION}/bin/mvn" ]; then
  wget -q "https://archive.apache.org/dist/maven/maven-3/${MAVEN_VERSION}/binaries/apache-maven-${MAVEN_VERSION}-bin.tar.gz" -O /tmp/maven.tgz
  tar xzf /tmp/maven.tgz -C /opt && rm /tmp/maven.tgz
fi
ln -sf "/opt/apache-maven-${MAVEN_VERSION}/bin/mvn" /usr/local/bin/mvn
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
# get.docker.com brings both plugins; an image whose docker came otherwise may lack them.
docker compose version >/dev/null 2>&1 || apt-get install -yq docker-compose-plugin
docker buildx version >/dev/null 2>&1 || apt-get install -yq docker-buildx-plugin
# node only: callers install pnpm themselves (pnpm/action-setup); a global pnpm in /usr/bin could
# not be hidden from a test that expects none on PATH.
command -v node >/dev/null || { curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && apt-get install -yq nodejs; }
# Playwright's browsers need system libraries that `playwright install --with-deps` would add with
# sudo. Installed here for every browser; the browsers themselves stay per job (and per cache).
npx -y playwright@latest install-deps
# gh, as on GitHub-hosted runners (release and pull-request steps call it).
if ! command -v gh >/dev/null; then
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg
  chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list
  apt-get update -q && apt-get install -yq gh
fi
if [ ! -x /opt/gradle-8.10/bin/gradle ]; then
  wget -q https://services.gradle.org/distributions/gradle-8.10-bin.zip -O /tmp/gradle.zip
  unzip -q /tmp/gradle.zip -d /opt/ && ln -sf /opt/gradle-8.10/bin/gradle /usr/local/bin/gradle && rm /tmp/gradle.zip
fi
id runner >/dev/null 2>&1 || useradd -m -s /bin/bash runner
usermod -aG docker runner
# `npm install -g` works without sudo, as on GitHub-hosted runners: the global prefix is /usr/local,
# whose node_modules and bin the runner owns.
npm config set prefix /usr/local --location=global
install -d -o runner -g runner /usr/local/lib/node_modules
chown runner:runner /usr/local/bin
RUNNER_VERSION=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | jq -r .tag_name | sed 's/^v//')
for i in 1 2 3 4; do
  d=/opt/actions-runner-$i
  if [ ! -x "$d/config.sh" ]; then
    mkdir -p "$d" && cd "$d"
    curl -fsSL -o runner.tgz "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
    tar xzf runner.tgz && rm runner.tgz
    chown -R runner:runner "$d"
  fi
  # Each slot's jobs get their own home: jobs running side by side on one server must not share
  # ~/setup-pnpm, ~/.npm, ~/.gradle or the Playwright cache (two pnpm setups at once break each
  # other). The runner applies .env to every job; config.sh at boot adds its own lines to this file.
  install -d -o runner -g runner "/home/runner/slot-$i"
  grep -q '^HOME=' "$d/.env" 2>/dev/null || echo "HOME=/home/runner/slot-$i" >> "$d/.env"
  # Gradle takes its user home from the JVM's user.home (the passwd entry), not from HOME.
  grep -q '^GRADLE_USER_HOME=' "$d/.env" || echo "GRADLE_USER_HOME=/home/runner/slot-$i/.gradle" >> "$d/.env"
  chown runner:runner "$d/.env"
done
# A bake that misses a tool fails here, before a snapshot is taken, not in someone's job later.
for t in java mvn gradle git jq shellcheck docker node npx python3 pip3 pipx gcc mysql sshpass xvfb-run zip rsync ssh gh; do
  command -v "$t" >/dev/null || { echo "bake: $t missing" >&2; exit 1; }
done
docker compose version >/dev/null && docker buildx version >/dev/null
mvn -v | grep -q "Apache Maven 3.9" || { echo "bake: mvn is not 3.9" >&2; exit 1; }
for i in 1 2 3 4; do
  grep -qx "HOME=/home/runner/slot-$i" "/opt/actions-runner-$i/.env" || { echo "bake: slot $i has no own HOME" >&2; exit 1; }
  grep -qx "GRADLE_USER_HOME=/home/runner/slot-$i/.gradle" "/opt/actions-runner-$i/.env" || { echo "bake: slot $i has no own GRADLE_USER_HOME" >&2; exit 1; }
done
locale -a | grep -qi '^en_US.utf8$' || { echo "bake: en_US.UTF-8 locale missing" >&2; exit 1; }
if [ "$(npm config get prefix)" != /usr/local ] || ! sudo -u runner test -w /usr/local/lib/node_modules; then
  echo "bake: npm global prefix not writable for runner" >&2; exit 1
fi
touch /opt/pool-baked
