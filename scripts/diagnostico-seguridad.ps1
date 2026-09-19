<#
    Diagnostico de seguridad del Bridge: recoge de una sola pasada los hechos
    del servidor que una revision de codigo no puede saber.

    SOLO LECTURA. No modifica nada, no para el servicio, no envia trafico a
    ningun broker ni al directorio publico, y NO IMPRIME NINGUN SECRETO: de las
    claves solo saca longitud y huella SHA-256 recortada, que es lo que hace
    falta para compararlas sin revelarlas.

    Uso (PowerShell como el usuario que ejecuta el Bridge):
        cd C:\ruta\al\Bridge
        powershell -ExecutionPolicy Bypass -File scripts\diagnostico-seguridad.ps1

    Para guardar la salida y poder pegarla:
        powershell -ExecutionPolicy Bypass -File scripts\diagnostico-seguridad.ps1 > diagnostico.txt
#>

param(
    [string]$Raiz = (Split-Path -Parent $PSScriptRoot),
    [string]$DestinoCopias = "D:\copias-bridge"
)

$ErrorActionPreference = "Continue"

function Titulo($texto) {
    Write-Output ""
    Write-Output ("=" * 72)
    Write-Output $texto
    Write-Output ("=" * 72)
}

function Huella($texto) {
    if ([string]::IsNullOrEmpty($texto)) { return "(vacio)" }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($texto)
    $hash = ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString("x2") }) -join ""
    return $hash.Substring(0, 16)
}

function LeerEnv($ruta) {
    $mapa = @{}
    if (-not (Test-Path $ruta)) { return $mapa }
    foreach ($linea in Get-Content $ruta) {
        $t = $linea.Trim()
        if ($t -eq "" -or $t.StartsWith("#")) { continue }
        $i = $t.IndexOf("=")
        if ($i -lt 1) { continue }
        $mapa[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1).Trim().Trim('"')
    }
    return $mapa
}

Write-Output "Diagnostico de seguridad del Bridge"
Write-Output ("Fecha: " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
Write-Output ("Raiz:  " + $Raiz)
Write-Output ("Host:  " + $env:COMPUTERNAME + "   Usuario: " + $env:USERNAME)

# ¿Es esta la maquina donde corre el Bridge de verdad? Sin esta comprobacion es
# facil ejecutar el diagnostico en el equipo de desarrollo, ver "no existe" por
# todas partes y sacar conclusiones tranquilizadoras sobre un servidor que ni
# siquiera se ha mirado.
$senales = @()
if (-not (Test-Path (Join-Path $Raiz ".env")))            { $senales += "no hay .env" }
if (-not (Test-Path (Join-Path $Raiz "data")))            { $senales += "no hay carpeta data" }
$hayUvicorn = $false
try {
    $hayUvicorn = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction Stop |
        Where-Object { $_.CommandLine -like "*uvicorn*" }).Count -gt 0
} catch { }
if (-not $hayUvicorn) { $senales += "no hay ningun proceso uvicorn" }
$hayTarea = $false
try { $hayTarea = @(Get-ScheduledTask -ErrorAction Stop | Where-Object { $_.TaskName -like "*Bridge*" }).Count -gt 0 } catch { }
if (-not $hayTarea) { $senales += "no hay tarea programada del Bridge" }

if ($senales.Count -ge 3) {
    Write-Output ""
    Write-Output ("!" * 72)
    Write-Output "ATENCION: esta NO parece la maquina donde corre el Bridge."
    Write-Output ("Motivos: " + ($senales -join "; ") + ".")
    Write-Output "El resto del informe describe ESTE equipo, no el servidor de"
    Write-Output "produccion. No sirve para dar por buena la configuracion de alla."
    Write-Output ("!" * 72)
}

# ---------------------------------------------------------------- 1. Claves
Titulo "1. Claves del .env (solo longitud y huella; NUNCA el valor)"

$envPath = Join-Path $Raiz ".env"
if (-not (Test-Path $envPath)) {
    Write-Output "NO EXISTE $envPath  <-- el Bridge no puede arrancar sin el"
} else {
    $cfg = LeerEnv $envPath
    # Huellas de los valores de ejemplo publicados en el repositorio.
    $ejemploEnv    = Huella "cambiame-por-una-cadena-larga-y-aleatoria"
    $ejemploReadme = Huella "cambia-esta-clave-bridge"

    foreach ($clave in @("BRIDGE_API_KEY", "CORE_API_KEY", "FERNET_KEY")) {
        $v = $cfg[$clave]
        if ($null -eq $v) {
            Write-Output ("{0,-16} AUSENTE" -f $clave)
            continue
        }
        $h = Huella $v
        $marca = ""
        if ($h -eq $ejemploEnv)    { $marca = "  <-- ES EL VALOR DE EJEMPLO DE .env.example. ROTALA." }
        if ($h -eq $ejemploReadme) { $marca = "  <-- ES EL VALOR DE EJEMPLO DEL README. ROTALA." }
        Write-Output ("{0,-16} longitud={1,-4} huella={2}{3}" -f $clave, $v.Length, $h, $marca)
    }

    if ($cfg["BRIDGE_API_KEY"] -and $cfg["CORE_API_KEY"] -and
        $cfg["BRIDGE_API_KEY"] -eq $cfg["CORE_API_KEY"]) {
        Write-Output "AVISO: BRIDGE_API_KEY y CORE_API_KEY son la MISMA cadena."
    }

    Titulo "2. Canal saliente hacia el Core y directorio de brokers"
    $url = $cfg["CORE_EVENTS_URL"]
    Write-Output ("CORE_EVENTS_URL = " + $(if ($url) { $url } else { "(no definida; el codigo usa http://localhost:8000/...)" }))
    if ($url -and $url.StartsWith("http://")) {
        $esLocal = ($url -match "://(localhost|127\.0\.0\.1|\[::1\])")
        if ($esLocal) {
            Write-Output "  -> http hacia loopback: aceptable, no sale de la maquina."
        } else {
            Write-Output "  -> http hacia un host REMOTO: la CORE_API_KEY viaja en claro. Pasar a https y rotarla."
        }
    } elseif ($url -and $url.StartsWith("https://")) {
        Write-Output "  -> https: correcto."
    }

    $dir = $cfg["BROKER_DIRECTORY_URL"]
    if ($null -eq $dir) {
        Write-Output "BROKER_DIRECTORY_URL no definida -> se usa el directorio publico por defecto (alta automatica ENCENDIDA)."
    } elseif ($dir.Trim() -eq "") {
        Write-Output "BROKER_DIRECTORY_URL vacia -> alta automatica de brokers APAGADA."
    } else {
        Write-Output ("BROKER_DIRECTORY_URL = " + $dir + "  (alta automatica ENCENDIDA)")
    }

    foreach ($v in @("ENABLE_DEBUG_SINK", "REPLAY_HISTORY_ON_CONNECT", "EMIT_TRADE_CANDLES", "SLOT_COUNT")) {
        Write-Output ("{0,-26} = {1}" -f $v, $(if ($cfg[$v]) { $cfg[$v] } else { "(por defecto)" }))
    }
}

# ------------------------------------------------------------------ 3. ACLs
Titulo "3. Permisos de los ficheros sensibles (quien puede leerlos y escribirlos)"

$objetivos = @(
    (Join-Path $Raiz ".env"),
    (Join-Path $Raiz "data"),
    (Join-Path $Raiz "data\slots.json"),
    (Join-Path $Raiz "terminals"),
    $DestinoCopias
)
foreach ($ruta in $objetivos) {
    Write-Output ""
    Write-Output ("--- " + $ruta)
    if (-not (Test-Path $ruta)) { Write-Output "    (no existe)"; continue }
    try {
        $acl = Get-Acl $ruta
        Write-Output ("    Propietario: " + $acl.Owner)
        foreach ($regla in $acl.Access) {
            Write-Output ("    {0,-40} {1,-6} {2}" -f $regla.IdentityReference, $regla.AccessControlType, $regla.FileSystemRights)
        }
    } catch {
        Write-Output ("    ERROR leyendo ACL: " + $_.Exception.Message)
    }
}

Write-Output ""
Write-Output "Cuentas locales en la maquina:"
try {
    Get-LocalUser | Select-Object Name, Enabled, LastLogon | Format-Table -AutoSize | Out-String | Write-Output
} catch {
    Write-Output "  (Get-LocalUser no disponible en esta version)"
}

# ------------------------------------------------------------- 4. Copias
Titulo "4. Copias de seguridad: donde estan y que contienen"

if (Test-Path $DestinoCopias) {
    $drive = (Get-Item $DestinoCopias).PSDrive
    Write-Output ("Unidad: " + $drive.Name + "  Proveedor: " + $drive.Provider)
    Write-Output ("Es unidad de red: " + $(if ($DestinoCopias -like "\\*") { "SI  <-- el aviso del script dice que no lo sea" } else { "no" }))
    Get-ChildItem $DestinoCopias -Directory -ErrorAction SilentlyContinue |
        Select-Object Name, CreationTime, LastWriteTime |
        Sort-Object Name | Format-Table -AutoSize | Out-String | Write-Output
    $ajenas = Get-ChildItem $DestinoCopias -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -notlike "bridge-backup-*" }
    if ($ajenas) {
        Write-Output "Carpetas que NO son bridge-backup-* en ese destino (la poda no las toca, pero conviene saber que estan):"
        $ajenas | ForEach-Object { Write-Output ("  " + $_.Name) }
    }
    $ultima = Get-ChildItem $DestinoCopias -Directory -Filter "bridge-backup-*" -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1
    if ($ultima) {
        Write-Output ("Contenido de la copia mas reciente (" + $ultima.Name + "):")
        Get-ChildItem $ultima.FullName -Recurse -File | ForEach-Object { Write-Output ("  " + $_.FullName.Substring($ultima.FullName.Length + 1) + "  (" + $_.Length + " bytes)") }
    }
} else {
    Write-Output ("No existe " + $DestinoCopias + " -> o la tarea de copia no esta puesta, o el destino es otro.")
}

# --------------------------------------------------- 5. Procesos y huerfanos
Titulo "5. Procesos: Workers vivos frente a lo que declara la API"

$procesos = @()
try {
    $procesos = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction Stop |
        Select-Object ProcessId, ParentProcessId, CommandLine
} catch {
    Write-Output ("No se pudo consultar Win32_Process: " + $_.Exception.Message)
}

$workers = $procesos | Where-Object { $_.CommandLine -like "*app.worker*" }
$uvicorn = $procesos | Where-Object { $_.CommandLine -like "*uvicorn*" }

Write-Output ("Procesos uvicorn (Manager): " + @($uvicorn).Count)
foreach ($p in $uvicorn) { Write-Output ("  PID " + $p.ProcessId) }
Write-Output ("Procesos Worker: " + @($workers).Count)
foreach ($p in $workers) {
    $slot = "?"
    if ($p.CommandLine -match "--slot-id\s+(\S+)") { $slot = $Matches[1] }
    $padreVivo = $uvicorn | Where-Object { $_.ProcessId -eq $p.ParentProcessId }
    $marca = ""
    if (-not $padreVivo) { $marca = "  <-- SIN Manager padre vivo: posible huerfano" }
    Write-Output ("  PID " + $p.ProcessId + "  padre " + $p.ParentProcessId + "  slot " + $slot + $marca)
}

$duplicados = $workers | ForEach-Object { if ($_.CommandLine -match "--slot-id\s+(\S+)") { $Matches[1] } } | Group-Object | Where-Object { $_.Count -gt 1 }
if ($duplicados) {
    Write-Output "SLOTS CON MAS DE UN WORKER (esto no deberia pasar nunca):"
    $duplicados | ForEach-Object { Write-Output ("  " + $_.Name + ": " + $_.Count + " procesos") }
}

Write-Output ""
Write-Output "Estado que declara la API (account_id recortado a 8 caracteres):"
$cfg2 = LeerEnv $envPath
if ($cfg2["BRIDGE_API_KEY"]) {
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:8088/api/v1/slots" -Headers @{ "X-API-Key" = $cfg2["BRIDGE_API_KEY"] } -TimeoutSec 10
        foreach ($s in $r.slots) {
            $acc = if ($s.account_id) { $s.account_id.Substring(0, [Math]::Min(8, $s.account_id.Length)) + "..." } else { "-" }
            Write-Output ("  {0,-9} {1,-12} worker_pid={2,-8} cuenta={3}" -f $s.slot_id, $s.status, $s.worker_pid, $acc)
        }
    } catch {
        Write-Output ("  No se pudo consultar la API local: " + $_.Exception.Message)
    }
} else {
    Write-Output "  (sin BRIDGE_API_KEY no se puede consultar)"
}

# ------------------------------------------------------------------ 6. Logs
Titulo "6. Logs: tamano, crecimiento y senales de fallo repetido"

$volumen = (Get-Item $Raiz).PSDrive
try {
    $disco = Get-PSDrive $volumen.Name
    Write-Output ("Volumen " + $volumen.Name + ": libre " + [math]::Round($disco.Free / 1GB, 2) + " GB de " + [math]::Round(($disco.Used + $disco.Free) / 1GB, 2) + " GB")
} catch { }

foreach ($carpeta in @((Join-Path $Raiz "data\logs"), (Join-Path $Raiz "logs"))) {
    Write-Output ""
    Write-Output ("--- " + $carpeta)
    if (-not (Test-Path $carpeta)) { Write-Output "    (no existe)"; continue }
    $ficheros = Get-ChildItem $carpeta -File -ErrorAction SilentlyContinue | Sort-Object Length -Descending
    Write-Output ("    ficheros: " + @($ficheros).Count + "   total: " + [math]::Round((($ficheros | Measure-Object Length -Sum).Sum) / 1MB, 2) + " MB")
    $ficheros | Select-Object -First 8 | ForEach-Object {
        Write-Output ("    {0,-28} {1,10} MB   ultima escritura {2}" -f $_.Name, [math]::Round($_.Length / 1MB, 2), $_.LastWriteTime)
    }
}

$logsSlot = Join-Path $Raiz "data\logs"
if (Test-Path $logsSlot) {
    Write-Output ""
    Write-Output "Recuento de senales (solo numeros, no se imprime ninguna linea):"
    foreach ($patron in @("error en poll", "no se pudo guardar", "no se pudo escribir", "reintentando por", "authorization failed", "Invalid account")) {
        $n = 0
        foreach ($f in Get-ChildItem $logsSlot -Filter "*.log" -File -ErrorAction SilentlyContinue) {
            $n += @(Select-String -Pattern ([regex]::Escape($patron)) -Path $f.FullName -ErrorAction SilentlyContinue).Count
        }
        Write-Output ("    {0,-26} {1}" -f $patron, $n)
    }
}

# ------------------------------------------------- 7. Historial de consola
Titulo "7. Historial de consola: ¿quedaron contrasenas de las sondas?"

try {
    $opt = Get-PSReadLineOption
    Write-Output ("HistorySaveStyle: " + $opt.HistorySaveStyle)
    Write-Output ("HistorySavePath:  " + $opt.HistorySavePath)
    if (Test-Path $opt.HistorySavePath) {
        $n = @(Select-String -Pattern "probe_slot|probe_history" -Path $opt.HistorySavePath -ErrorAction SilentlyContinue).Count
        Write-Output ("Invocaciones de las sondas registradas en el historial: " + $n)
        if ($n -gt 0) {
            Write-Output "  <-- si alguna llevaba --password de una cuenta de cliente, ROTA esa credencial"
            Write-Output "      y limpia el historial. El contenido NO se imprime aqui a proposito."
        }
        $aclH = Get-Acl $opt.HistorySavePath
        Write-Output ("Permisos del historial (propietario " + $aclH.Owner + "):")
        foreach ($regla in $aclH.Access) {
            Write-Output ("    {0,-40} {1,-6} {2}" -f $regla.IdentityReference, $regla.AccessControlType, $regla.FileSystemRights)
        }
    } else {
        Write-Output "El fichero de historial no existe."
    }
} catch {
    Write-Output ("PSReadLine no disponible: " + $_.Exception.Message)
}

# ------------------------------------------------------- 8. Red y tareas
Titulo "8. Exposicion del puerto 8088 y tareas programadas"

try {
    $reglas = Get-NetFirewallRule -Enabled True -Direction Inbound -ErrorAction Stop |
        Where-Object { $_.Action -eq "Allow" }
    $delPuerto = @()
    foreach ($r in $reglas) {
        $pf = $r | Get-NetFirewallPortFilter -ErrorAction SilentlyContinue
        if ($pf -and ($pf.LocalPort -contains "8088" -or $pf.LocalPort -eq "8088")) { $delPuerto += $r }
    }
    if ($delPuerto) {
        foreach ($r in $delPuerto) {
            $af = $r | Get-NetFirewallAddressFilter -ErrorAction SilentlyContinue
            $pf2 = $r | Get-NetFirewallPortFilter -ErrorAction SilentlyContinue
            $ap = $r | Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue
            # DisplayName puede venir sin resolver ("ms-resource:AppTitle"), asi
            # que se imprime tambien el nombre interno y el programa.
            Write-Output ("Regla: " + $r.DisplayName + "   [Name: " + $r.Name + "]")
            Write-Output ("   Puertos:       " + ($pf2.LocalPort -join ", "))
            Write-Output ("   Programa:      " + $(if ($ap.Program) { $ap.Program } else { "(cualquiera)" }))
            Write-Output ("   RemoteAddress: " + ($af.RemoteAddress -join ", "))
            if ($af.RemoteAddress -contains "Any") {
                Write-Output "   <-- ABIERTO A CUALQUIER ORIGEN. Deberia estar limitado a la IP del Core."
            }
        }
    } else {
        Write-Output "No hay ninguna regla de entrada que nombre el 8088."
        Write-Output "Puede significar que esta cerrado por la regla por defecto, o que hay una regla mas amplia."
    }
} catch {
    Write-Output ("No se pudieron leer las reglas de firewall: " + $_.Exception.Message)
}

Write-Output ""
Write-Output "Quien escucha en el 8088:"
try {
    Get-NetTCPConnection -LocalPort 8088 -State Listen -ErrorAction Stop |
        Select-Object LocalAddress, LocalPort, OwningProcess | Format-Table -AutoSize | Out-String | Write-Output
} catch {
    Write-Output "  (nadie, o no se pudo consultar)"
}

Write-Output "Tareas programadas del Bridge:"
try {
    Get-ScheduledTask | Where-Object { $_.TaskName -like "*Bridge*" } |
        Select-Object TaskName, State, @{N = "Usuario"; E = { $_.Principal.UserId } }, @{N = "Nivel"; E = { $_.Principal.RunLevel } } |
        Format-Table -AutoSize | Out-String | Write-Output
} catch {
    Write-Output ("  " + $_.Exception.Message)
}

Titulo "Fin del diagnostico"
Write-Output "Nada de lo anterior ha modificado el sistema."
Write-Output "Ningun secreto se ha impreso: de las claves solo salen longitud y huella."
