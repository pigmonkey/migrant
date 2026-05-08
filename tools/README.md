# tools/

Developer and operator tools for working with migrant-managed VMs.

---

## netcheck.py

A network connectivity probe that runs **inside** a migrant-managed VM.
Use it to verify that isolation and connectivity settings match your
Migrantfile expectations before handing the VM to an agent.

Requires Python 3.11+ and `uv`. Both are available on all example VM distros
(Arch, Debian, Ubuntu) after provisioning.

### Getting the script into the VM

Copy it into your shared workspace:

```bash
cp tools/netcheck.py /path/to/your/vm-dir/workspace/
```

Or add a task to your Ansible playbook to provision it automatically.

### Usage

```bash
# Interactive (prompts for optional LAN/host-port/peer inputs)
./netcheck.py

# Non-interactive (automatic tests only)
./netcheck.py --no-interactive

# Check tool availability before running
./netcheck.py --check-tools

# Supply optional inputs via flags
./netcheck.py --lan-ip 192.168.1.50 --host-port tcp/9999 --peer-ip 192.168.200.5
```

### What it checks

| Category    | Tests                                                                                         |
| ----------- | --------------------------------------------------------------------------------------------- |
| Inventory   | Interfaces, routing tables, DNS resolvers, gateway                                            |
| DNS         | Default resolver (A/AAAA), direct queries to 8.8.8.8/1.1.1.1, interception detection, whoami |
| Internet    | HTTP/HTTPS reachability, IPv6 probe, public IP/VPN info, traceroute, MTU                      |
| Isolation   | Gateway ping/TCP, RFC1918 TCP probes, IPv6 external                                           |
| Host access | TCP/UDP connect to a host port (requires `allow-host-port` in Migrantfile)                    |
| LAN / peer  | Ping and TCP to a LAN host, peer VM ping                                                      |

Exit code 0 if all tests produce expected results; 1 if any test with a
defined expectation produces an unexpected result.
