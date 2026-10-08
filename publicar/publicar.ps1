# publicar.ps1 - Sube ClassHelper a GitHub y monta el perfil.
#
# Solo hace falta iniciar sesion en GitHub UNA vez (se abre el navegador).
# Usa el gestor de credenciales de Git, asi que la clave no se escribe en ningun sitio.
# Firma los commits con tu email anonimo de GitHub (...@users.noreply.github.com).

$ErrorActionPreference = "Stop"
# UTF-8 SIN BOM al pasar texto a git (con BOM, git no reconoce "protocol=https")
$OutputEncoding = New-Object System.Text.UTF8Encoding $false
$repoDir = Split-Path $PSScriptRoot -Parent
Set-Location $repoDir

function Paso($t) { Write-Host "`n==> $t" -ForegroundColor Cyan }

# 1) Seguridad: nada privado en lo que se va a subir
Paso "Comprobando que no se sube nada privado"
$tracked = git ls-files
foreach ($f in @("config.json", ".env")) { if ($tracked -contains $f) { throw "$f esta en el repositorio: abortado" } }
$hits = git grep -n -I -E "AIza[0-9A-Za-z_-]{20,}|sk-[A-Za-z0-9]{20,}" -- . 2>$null
if ($hits) { $hits; throw "Hay algo que parece una clave de API: abortado" }
Write-Host "   OK: ni config.json, ni .env, ni claves"

# 2) Iniciar sesion (el navegador se abre si hace falta)
Paso "Iniciando sesion en GitHub (mira el navegador)"
$cred = "protocol=https`nhost=github.com`n`n" | git credential fill
$token = ($cred | Where-Object { $_ -like "password=*" }) -replace "^password=", ""
if (-not $token) { throw "No se pudo iniciar sesion en GitHub" }
# Guardar la sesion: sin esto, el push vuelve a pedir credenciales y falla
$cred | git credential approve
$h = @{ Authorization = "token $token"; Accept = "application/vnd.github+json"; "User-Agent" = "ClassHelper-publish" }
$me = Invoke-RestMethod -Uri "https://api.github.com/user" -Headers $h
$login = $me.login
Write-Host "   Sesion iniciada como $login"

# 3) Autor de los commits: el email anonimo de GitHub
$email = "$($me.id)+$login@users.noreply.github.com"
git config user.name $login
git config user.email $email
if ((git log -1 --format="%ae") -ne $email) { git commit --amend --reset-author --no-edit | Out-Null }
Write-Host "   Commits firmados como $login <$email>"

# 4) Crear el repositorio (si no existe) y subir
Paso "Creando el repositorio ClassHelper"
try { Invoke-RestMethod -Uri "https://api.github.com/repos/$login/ClassHelper" -Headers $h | Out-Null; Write-Host "   Ya existia" }
catch {
  $body = @{ name = "ClassHelper"; private = $false; has_wiki = $false
             description = "Graba la clase, sigue las diapositivas y estudia con IA. Transcripcion 100% local con Whisper." } | ConvertTo-Json
  Invoke-RestMethod -Method Post -Uri "https://api.github.com/user/repos" -Headers $h -Body $body | Out-Null
  Write-Host "   Creado"
}
$topics = @{ names = @("whisper", "speech-to-text", "pyside6", "study-tool", "flashcards", "students", "transcription", "windows") } | ConvertTo-Json
Invoke-RestMethod -Method Put -Uri "https://api.github.com/repos/$login/ClassHelper/topics" -Headers $h -Body $topics | Out-Null
if (-not (git remote 2>$null | Select-String -Quiet "origin")) { git remote add origin "https://github.com/$login/ClassHelper.git" }
git push -u origin main
if ($LASTEXITCODE) { throw "No se pudo subir ClassHelper (git push fallo)" }
Write-Host "   Subido: https://github.com/$login/ClassHelper"

# 5) Perfil: repositorio especial <login>/<login> con README
Paso "Montando tu perfil de GitHub"
try { Invoke-RestMethod -Uri "https://api.github.com/repos/$login/$login" -Headers $h | Out-Null }
catch {
  $body = @{ name = $login; private = $false; description = "Mi perfil" } | ConvertTo-Json
  Invoke-RestMethod -Method Post -Uri "https://api.github.com/user/repos" -Headers $h -Body $body | Out-Null
}
$tmp = Join-Path $env:TEMP "perfil_$login"
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
New-Item -ItemType Directory $tmp | Out-Null
(Get-Content (Join-Path $PSScriptRoot "perfil_README.md") -Raw -Encoding UTF8) -replace "\{\{LOGIN\}\}", $login |
  Set-Content (Join-Path $tmp "README.md") -Encoding UTF8
Push-Location $tmp
git init -b main | Out-Null
git config user.name $login; git config user.email $email
git add README.md; git commit -m "Perfil" | Out-Null
git remote add origin "https://github.com/$login/$login.git"
git push -u origin main --force
$fallo = $LASTEXITCODE
Pop-Location
if ($fallo) { throw "No se pudo subir el perfil (git push fallo)" }
Write-Host "   Perfil listo: https://github.com/$login"

Paso "Hecho"
Start-Process "https://github.com/$login"
