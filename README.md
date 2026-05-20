# Shanty Monitor

Home lab monitoring dashboard for blog.theshanty.us/dashboard

## Architecture

```
[agent hosts] --HTTP POST--> [collector :2777] --writes--> [metrics.json]
[probe-only hosts] <--ping/port-- [collector]                    |
[external domains] <--HTTPS-- [collector]                        v
                                                         [dashboard HTML]
                                                    (Apache basic auth, HTTPS)
```

## Quick Start

### 1. Collector (192.168.1.168)

Install dependencies:
```bash
sudo apt install python3-pip
pip3 install flask pyyaml requests --break-system-packages
```

Edit config:
```bash
sudo mkdir -p /etc/shanty-monitor /var/lib/shanty-monitor
sudo cp collector/config.yaml /etc/shanty-monitor/config.yaml
sudo vi /etc/shanty-monitor/config.yaml
```

Key things to set in config.yaml:
- `collector.token` — change from default, use the same value in all agents
- `probe_only_hosts` — fill in real IPs for homeassistant, flightaware, etc.
- `collector.json_output` — must match your Apache webroot (default: /var/www/html/dashboard/metrics.json)

Deploy collector:
```bash
sudo mkdir -p /opt/shanty-monitor/collector
sudo cp collector/collector.py /opt/shanty-monitor/collector/
sudo cp collector/shanty-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now shanty-collector
sudo journalctl -u shanty-collector -f
```

### 2. Dashboard (Apache)

Create dashboard directory:
```bash
sudo mkdir -p /var/www/html/dashboard
sudo cp dashboard/index.html /var/www/html/dashboard/
```

Set up basic auth:
```bash
sudo mkdir -p /etc/shanty-monitor
sudo htpasswd -c /etc/shanty-monitor/.htpasswd yourusername
```

Add Apache config (inside your blog.theshanty.us VirtualHost):
```bash
sudo cp apache/shanty-monitor.conf /etc/apache2/conf-available/
sudo a2enconf shanty-monitor
sudo apache2ctl configtest && sudo systemctl reload apache2
```

Make sure mod_authz_core is enabled:
```bash
sudo a2enmod auth_basic authn_file authz_user
```

### 3. Agents (all SSH-accessible hosts)

Edit agent/agent.yaml — set the token to match collector config.

Deploy to all agent hosts at once:
```bash
./install.sh --all
```

Or one at a time:
```bash
./install.sh pi@192.168.1.x
./install.sh user@mail.theshanty.us
```

### 4. Verify

- Dashboard: https://blog.theshanty.us/dashboard
- Collector health: curl http://192.168.1.168:2777/api/health
- Agent logs on any host: sudo journalctl -u shanty-agent -f
- Collector logs: sudo journalctl -u shanty-collector -f

## File Layout

```
shanty-monitor/
├── README.md
├── install.sh                  # deploy agent to hosts via SSH
├── agent/
│   ├── agent.py                # runs on each managed host
│   ├── agent.yaml              # agent config template
│   └── shanty-agent.service    # systemd unit
├── collector/
│   ├── collector.py            # Flask collector + prober
│   ├── config.yaml             # host inventory + settings
│   └── shanty-collector.service
├── dashboard/
│   └── index.html              # self-contained dashboard UI
└── apache/
    └── shanty-monitor.conf     # Apache snippet
```

## Updating

To push an updated agent.py to all hosts:
```bash
./install.sh --all
```

To update the dashboard:
```bash
sudo cp dashboard/index.html /var/www/html/dashboard/
```

## Adding a New Host

**Agent host** — add to `agent_hosts` in config.yaml, then run install.sh:
```yaml
- name: newpi
  ip: 192.168.1.xxx
  ports: [22]
  tags: [local]
```
```bash
./install.sh pi@192.168.1.xxx
sudo systemctl restart shanty-collector
```

**Probe-only host** — add to `probe_only_hosts` in config.yaml:
```yaml
- name: newdevice
  ip: 192.168.1.xxx
  ports: [80]
  tags: [iot, local]
```
Then restart the collector:
```bash
sudo systemctl restart shanty-collector
```
