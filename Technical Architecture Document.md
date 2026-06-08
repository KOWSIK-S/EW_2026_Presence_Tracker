# 📄 Technical Architecture Document: Presence Gateway
####**Currently in Iteration 1**
## 1. Executive Summary: Version History Overview

* **Iteration 1 (The Monolithic Prototype):** A synchronous, single-device tracker utilizing ICMP pings against a static IP and localized ARP cache lookups, bundled with a basic Flask UI.
* **Iteration 2 (The Asynchronous Scanner):** Introduced `asyncio` and semaphore-gated subnet sweeping to dynamically track devices via DHCP, removing the static IP limitation.
* **Iteration 3 (The Relational Gateway):** Transitioned to a persistent JSON database, separating the UI into a secure Waitress WSGI Admin portal and a Viewer dashboard, while adding interactive Telegram controls.
* **Iteration 4 (The Enterprise Edge Node):** Implemented session analytics, ambient rogue device discovery, hardware-level failsafe hooks, and SD-card optimized atomic database commits.

---

## 2. Deep Architectural Reviews

### Iteration 1: The Monolithic Prototype

**Architectural Paradigm:** Synchronous Polling Loop with Global State
**Primary Objective:** Establish a baseline proof-of-concept for hybrid network/radio presence detection.

The initial iteration of the Presence Tracker was built on a rudimentary, highly coupled monolithic architecture. The core logic relied on a `while True` loop running in a background daemon thread, isolated from the primary Flask web server thread. The primary detection mechanism relied on a fundamental quirk of Linux networking: the Address Resolution Protocol (ARP) cache. Modern smartphones (iOS and Android) enter deep-sleep states to conserve battery, causing them to drop off traditional network scans. To circumvent this, the script executed a targeted `ping` (ICMP Echo Request) to a hardcoded static IP address. Even if the phone did not explicitly respond to the ICMP packet, the router and switch hardware would route the packet, forcing the target device's network interface controller (NIC) to wake up and acknowledge the routing. This acknowledgment forces the Raspberry Pi's Linux kernel to update its `/proc/net/arp` table with the device's MAC address.

The script then synchronously read the system's ARP table file to verify the presence of the hardcoded MAC address. If the Wi-Fi scan failed, the system fell back to Bluetooth Low Energy (BLE) scanning using the `bleak` library. This hybrid approach was the system's greatest strength, creating redundancy. However, the synchronous nature of the design was a critical bottleneck. The `asyncio.run(scan_ble())` call was invoked sequentially, meaning the entire tracking engine paused while waiting for radio hardware timeouts.

Furthermore, data state was entirely ephemeral. Presence states (`is_child_home`) and the event log (`system_logs`) were stored in standard Python variables. If the Raspberry Pi lost power or the Python process was restarted, all historical data was irrevocably lost. The UI was equally primitive, utilizing Flask's built-in development server (Werkzeug) which is fundamentally unsafe for production environments due to its lack of thread safety and vulnerability to simple denial-of-service (DoS) conditions. Security was virtually non-existent, with critical secrets (Telegram bot tokens, Chat IDs) hardcoded as plain-text strings directly into the executable Python file.

### Iteration 2: The Asynchronous Subnet Scanner

**Architectural Paradigm:** Concurrent Subprocess Execution
**Primary Objective:** Eliminate the static IP requirement through rapid, concurrent subnet discovery.

The second major phase of development addressed the most significant operational flaw of the prototype: the reliance on a static IP address. In modern home networks, DHCP leases expire and IP addresses rotate. To solve this without relying on complex router-level integrations, the architecture was heavily refactored to utilize Python's `asyncio` library to perform a "brute-force" sweep of the entire local `/24` subnet (254 potential IP addresses).

Because executing 254 sequential pings would take several minutes, the code introduced a highly optimized parallel execution model. By utilizing `asyncio.create_subprocess_exec`, the Python application spawned native OS-level ping commands asynchronously. To prevent the Raspberry Pi from exhausting its file descriptors or causing a localized network broadcast storm that could crash cheap consumer routers, an `asyncio.Semaphore(50)` was introduced. This bounded concurrency pattern ensured that exactly 50 ICMP requests were "in-flight" at any given microsecond.

This iteration also marked the beginning of proper security hygiene. Hardcoded secrets were removed from the source code entirely. The application transitioned to consuming environment variables parsed from a local Linux `.env` file or injected via `systemd` service configurations. A fail-fast initialization block was written to immediately crash the script with a `ValueError` upon boot if these secrets were missing, adhering to the fail-safe design philosophy.

However, this iteration retained significant technical debt. The core engine still recreated and destroyed `asyncio` event loops on every pass of the `while True` loop via `asyncio.run()`, a highly inefficient operation that causes memory fragmentation over time. The system still lacked persistence, and the web interface remained completely unauthenticated, meaning anyone on the local network could view the presence logs.

### Iteration 3: The Multi-Node Relational Gateway

**Architectural Paradigm:** Thread-Safe WSGI with Persistent JSON State
**Primary Objective:** Transition to a production-ready, multi-tenant system with authenticated routing.

This iteration represents the transition from a "script" to a "system." The application was fundamentally re-architected to support multiple devices, multiple users, and persistent data. The ephemeral global variables were replaced with a localized JSON flat-file database (`tracker_db.json`). Because multiple threads (the Web UI, the Telegram bot, the async scanner) needed concurrent access to this data, a strict `threading.Lock()` was introduced. Every read and write to the database was gated behind this mutex, ensuring thread safety and preventing race conditions that could corrupt the JSON structure.

The networking layer was upgraded. The Flask application was split into two distinct logical servers: a read-only Viewer on port 5000, and an Admin dashboard on port 5001. Crucially, Flask's internal development server was stripped out and replaced with `waitress`, a production-grade Web Server Gateway Interface (WSGI) capable of handling concurrent requests efficiently. The Admin dashboard introduced custom PIN-based authentication backed by Flask's cryptographically signed session cookies, protected by a dedicated `SECRET_KEY`.

A massive upgrade was applied to the Telegram notification engine. Instead of a hardcoded single admin, a Role-Based Access Control (RBAC) model was engineered. The database now stored a relational map between "Devices" and "Viewer IDs". This allowed the system to perform targeted routing—for example, sending alerts about Device A only to Viewer X, while Device B went to Viewer Y. To prevent the tracking engine from stalling during slow Telegram API HTTP requests, a thread-safe `queue.Queue()` was implemented. The tracking engine now instantly drops alert payloads into the queue and resumes scanning, while a dedicated background daemon thread pops items off the queue and handles the network I/O required to message the Telegram API.

### Iteration 4: The Stabilised Enterprise Edge Node

**Architectural Paradigm:** Fault-Tolerant Edge Computing with Time-Series Analytics
**Primary Objective:** Maximize hardware lifespan, provide deep metrics, and ensure system survivability.

The final architectural iteration focused on edge-case survivability and data intelligence. The most critical underlying change was to the database I/O layer. On constrained hardware like a Raspberry Pi operating on an SD card, continuous disk writes cause rapid wear-leveling failure. Earlier enterprise drafts utilized `os.fsync()` to force the hardware controller to flush data immediately, but this caused severe write-amplification. The final stabilized build transitioned to an atomic `os.replace()` strategy. This creates a temporary file, writes the JSON payload, and swaps it with the original file via a native kernel-level atomic pointer swap. This guarantees that even if power is yanked mid-write, the original database is never left in a corrupted, half-written state, while still allowing the Linux kernel to optimize disk buffering.

The application introduced an advanced Time-Series Analytics Engine. Instead of just logging "Arrived" and "Departed," the Python backend now loops through the raw text arrays dynamically to reconstruct session objects. It uses `datetime` and `timedelta` modules to calculate cumulative monthly presence hours, average stay durations, and maximum session lengths. This allows the system to act as a proper behavioral analytics tool without requiring the overhead of installing a heavy database like PostgreSQL or InfluxDB on the Pi.

Finally, "Ambient Discovery" was introduced. By comparing the live ARP/BLE sweep results against the registered database of known devices, the system can dynamically flag and list "Rogue" or "Untracked" devices currently occupying the physical airspace or network. The Telegram Bot was upgraded from a static notification pusher to a fully interactive control terminal, utilizing `pyTelegramBotAPI` to render inline keyboard menus, allowing the admin to force network scans, query the database, or remotely reboot the tracking engine without SSH access.

---

## 3. Future Improvements (Hardware & Software)

### Hardware Scaling

1. **M.2 NVMe Storage Integration:** SD cards are fundamentally unsuited for database-driven edge computing. Future deployments should utilize a Raspberry Pi 5 with an NVMe baseboard, or boot via a high-quality USB 3.0 SSD. This would allow the system to write high-frequency analytics without fear of hardware failure.
2. **Dedicated BLE Beacon Nodes (ESP32):** Relying solely on the Raspberry Pi's internal Bluetooth antenna limits the tracking radius to roughly 30 feet. Future architectures should integrate with external ESP32 microcontrollers running *ESPresense*. These cheap nodes can be placed in every room, feeding MQTT messages back to the Pi, creating a mesh network capable of pinpointing a device down to a specific room, rather than just "Home" or "Away".
3. **PoE (Power over Ethernet):** To ensure the Gateway never loses power or network connection, a PoE HAT should be attached, drawing stabilized power directly from a managed switch alongside an Uninterruptible Power Supply (UPS).

### Software Scaling

1. **Migration to SQLite/PostgreSQL:** While the flat-file JSON atomic swap works for hundreds of logs, it becomes an O(N) bottleneck as data scales into the tens of thousands of rows. Transitioning to `SQLite` using an ORM like `SQLAlchemy` would allow for instantaneous queries, indexed log searching, and drastically lower RAM consumption.
2. **WebSockets for Live UI:** Currently, the Viewer and Admin dashboards rely on manual page refreshes or standard HTTP GET requests. Implementing `Flask-SocketIO` would allow the backend to push state changes to the browser in real-time, instantly turning the dashboard green the millisecond a device is detected, eliminating polling latency.
3. **Integration with Home Assistant (MQTT):** The tracker should not exist in a vacuum. By implementing an MQTT publishing client (using `paho-mqtt`), the Python script could broadcast presence states to a broader smart home ecosystem like Home Assistant. This would allow presence detection to physically trigger smart lights, HVAC systems, or security alarms.

---

## 4. Missed Concepts & Blind Spots

While the application is robust, there are several networking and architectural concepts omitted from the current design that limit its ultimate efficacy:

**1. 802.11w MAC Randomization (Privacy Features)**
The entire Wi-Fi tracking engine relies on OSI Layer 2 MAC addresses. However, modern iterations of iOS and Android utilize aggressive MAC randomization (Private Wi-Fi Address) by default. Every time the phone disconnects and reconnects, it may present a different MAC address to the router. The current codebase requires the user to manually disable this security feature on their phone for their home network. A more advanced concept would involve tracking devices via mDNS (Bonjour/Zeroconf) hostnames or localized SSL certificate fingerprinting, which remain static even if the MAC address rotates.

**2. IPv6 Network Tracking**
The `nmap` and `ping` sweep logic hardcodes the assumption that the local area network is operating exclusively on IPv4 (the `134.324.x.x` space). Many modern ISPs and consumer routers are transitioning to IPv6 internal routing (SLAAC). The ICMP sweep logic completely ignores IPv6 Neighbor Discovery Protocol (NDP), meaning a device utilizing only an IPv6 local link address is entirely invisible to this tracker.

**3. Containerization (Docker)**
The setup instructions dictate installing packages globally via `apt` and running a native Python virtual environment. In modern DevOps, this is considered an anti-pattern due to dependency conflicts. The application should be packaged into a `Dockerfile` with a `docker-compose.yml` manifest. This would ensure that the required versions of `nmap`, `bluez`, and Python libraries are perfectly replicated on any hardware, drastically lowering the deployment friction for new users.

**4. Log Rotation and Data Pruning**
The database utilizes a crude array slicing technique (`logs[:1000]`) to prevent infinite file growth. However, this deletes data purely based on count, not time. If a device rapidly connects/disconnects 1,000 times in a day due to poor signal, months of historical data are instantly truncated. The system lacks a proper log rotation mechanism (like Linux `logrotate` or Python's `TimedRotatingFileHandler`) to archive old data to compressed `.gz` files for long-term historical auditing.
