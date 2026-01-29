# Port Forwarding Guide - Expose PoliceTracker to Internet

## Overview

PoliceTracker Web Server runs on **port 8892** by default. To make it accessible from the internet, you need to forward this port on your router.

## Steps

### 1. Find Your Mac's Local IP Address

```bash
# On your Mac, run:
ifconfig | grep "inet " | grep -v 127.0.0.1
```

Look for something like `192.168.1.XXX` or `10.0.0.XXX` - this is your Mac's local IP.

### 2. Access Your Router Admin Panel

- Usually at `http://192.168.1.1` or `http://10.0.0.1`
- Check router documentation for default IP
- Login with admin credentials

### 3. Set Up Port Forwarding

1. Navigate to **Port Forwarding** or **Virtual Server** section
2. Add a new rule:
   - **Service Name**: PoliceTracker
   - **External Port**: 8892 (or any port you prefer)
   - **Internal Port**: 8892
   - **Internal IP**: Your Mac's local IP (from step 1)
   - **Protocol**: TCP
   - **Status**: Enabled

3. Save the configuration

### 4. Find Your Public IP

```bash
# Check your public IP:
curl ifconfig.me
```

Or visit: https://whatismyipaddress.com

### 5. Access from Internet

Once port forwarding is set up, access the dashboard at:
```
http://YOUR_PUBLIC_IP:8892
```

**Note**: If your public IP changes (dynamic IP), you may want to set up a dynamic DNS service (like No-IP or DuckDNS) to get a stable hostname.

## Security Considerations

⚠️ **Warning**: Exposing the dashboard to the internet makes it publicly accessible. Consider:

1. **Add Authentication** (future enhancement)
2. **Use HTTPS** (requires SSL certificate)
3. **Firewall Rules** - Only allow specific IPs if possible
4. **VPN Alternative** - Consider using a VPN instead of direct exposure

## Testing

1. Start the web server:
   ```bash
   ./start_web_server.sh
   ```

2. Test locally first:
   ```bash
   curl http://localhost:8892/api/health
   ```

3. Test from another device on your network:
   ```
   http://YOUR_MAC_LOCAL_IP:8892
   ```

4. Test from internet (after port forwarding):
   ```
   http://YOUR_PUBLIC_IP:8892
   ```

## Troubleshooting

- **Can't access from internet**: Check firewall on Mac (System Settings > Network > Firewall)
- **Port already in use**: Change `WEB_PORT` in `start_web_server.sh` or set `WEB_PORT=XXXX` environment variable
- **Router doesn't support port forwarding**: Consider using a service like ngrok for temporary tunneling
