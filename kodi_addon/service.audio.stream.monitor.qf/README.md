# service.audio.stream.monitor.qf

Kodi service addon that exposes `provider_finder` results to Audio Stream Monitor (ASM)
via Window `10000` properties.

## Scope
- Keeps existing `provider_finder` GUI unchanged (`python main.py`).
- Adds an ASM bridge service with request/response contract.
- Default state is enabled via setting `provider_finder_enabled=true`.
- Addon is self-contained (no external `provider_finder_project_path` setting required).
- On successful hits, ASM-QF writes verified sources into ASM shared DB (`verified_station_sources`).

## ASM Contract
Request properties:
- `RadioMonitor.QF.Request.Id`
- `RadioMonitor.QF.Request.Station`
- `RadioMonitor.QF.Request.Mode`
- `RadioMonitor.QF.Request.Ts`

Response properties:
- `RadioMonitor.QF.Response.Id`
- `RadioMonitor.QF.Response.Status`
- `RadioMonitor.QF.Response.Artist`
- `RadioMonitor.QF.Response.Title`
- `RadioMonitor.QF.Response.Source`
- `RadioMonitor.QF.Response.Reason`
- `RadioMonitor.QF.Response.Meta`
- `RadioMonitor.QF.Response.Ts`

Rules:
- Process only when `Request.Id` changes.
- Response is valid only if `Response.Id == Request.Id`.

## Deploy
Run:

```powershell
powershell -ExecutionPolicy Bypass -File .\kodi_addon\deploy_service_audio_stream_monitor_qf.ps1
```

## Install on demand from ASM
Yes, this is possible, but it must be triggered from ASM side (not from this addon itself),
for example by calling:

```text
InstallAddon(service.audio.stream.monitor.qf)
```

Then ASM can set `RadioMonitor.QF.Request.*` properties and read `RadioMonitor.QF.Response.*`.
