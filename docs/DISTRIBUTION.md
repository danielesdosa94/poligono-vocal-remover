# Distribución — Polígono AI Hub

Cómo se construye el instalador, qué pesa, y qué hay que decirle al cliente
en la página de descarga. Escrito para quien hace el build y para quien
redacta la ficha de producto.

Las decisiones de arquitectura están en `docs/REFACTOR_PLAN.md`; las reglas
de trabajo, en `CLAUDE.md`.

---

## 1. Construir

```powershell
cd "C:\Users\Expert Pro\Desktop\Daniel\poligono-vocal-remover"
.\scripts\build.ps1
```

El script hace, en este orden: comprueba el venv y que `ffmpeg.exe` y
`ffprobe.exe` estén en `resources\bin\ffmpeg\`, compila `motor.exe` con
PyInstaller, le manda un `ping` y verifica que conteste `pong`, corre
electron-builder, e imprime el tamaño del instalador.

| Opción | Para qué |
|---|---|
| `-SkipMotor` | Reutiliza `dist\motor`. La mitad Python es la lenta; esto es lo que se usa iterando sobre el empaquetado de Electron. |
| `-DebugConsole` | `motor.exe` con ventana de consola visible, para ver el tráfico JSON cuando el daemon congelado se porta mal. |
| `-Clean` | Borra `dist\motor`, `build\pyinstaller` y `release\` antes de empezar. |

A mano, si hiciera falta:

```powershell
.\python\venv\Scripts\Activate.ps1
pyinstaller python\motor.spec --workpath build\pyinstaller --noconfirm
npm run dist:win
```

**El directorio de trabajo importa.** PyInstaller resuelve `--distpath` y
`--workpath` contra el directorio actual, y `package.json` espera el
resultado en `dist\motor`. Siempre desde la raíz del repo. El `--workpath`
explícito evita que PyInstaller ensucie `build\`, que electron-builder lee
como su `buildResources`.

### El build produce DOS archivos, y hay que subir los dos

```
release\nsis-web\Poligono-AI-Hub-1.1.0-Setup.exe      0.74 MB   <- lo que descarga el cliente
release\nsis-web\poligono-ai-hub-1.1.0-x64.nsis.7z    2.21 GB   <- lo que descarga el instalador
```

Ojo con el subdirectorio: `nsis-web` escribe en `release\nsis-web\`, no en
`release\`. `build.ps1` borra los artefactos viejos antes de construir y se
niega a nombrar ninguno si encuentra más de un instalador, porque un `.exe`
huérfano de un build anterior es indistinguible a simple vista del bueno.

El target es **`nsis-web`**, no `nsis`. El instalador no lleva la aplicación
dentro: la descarga durante la instalación desde la URL que tiene grabada.

**Por qué.** La aplicación comprimida son 2.21 GB y `makensis.exe` es un
proceso de **32 bits**: no puede mapear en memoria un archivo de ese tamaño en
un espacio de direcciones de 2 GB. Falla con

```
File: failed creating mmap of "...\poligono-ai-hub-1.1.0-x64.nsis.7z"
```

No es configurable: NSIS tampoco soporta instaladores de más de 2 GB aunque
pudiera mapearlo. Y de los 4.81 GB sin comprimir, **2.8 GB son DLL de CUDA que
son dependencias de carga de `torch_cuda.dll`** y no se pueden quitar sin
romper la GPU (`cublasLt` 643 MB, `cusparse` 362 MB, `cufft` 263 MB,
`cusolver` 215 MB, `cublas` 108 MB, más `torch_cuda` 981 MB y `torch_cpu`
244 MB). Podando lo demás no se bajaba de 2 GB con margen sano.

### La URL del paquete

`build.nsisWeb.appPackageUrl` en `package.json` es la carpeta donde se va a
alojar el `.nsis.7z`, **con barra final**:

```json
"appPackageUrl": "https://tu-hosting/aihub/"
```

El instalador pedirá `<appPackageUrl><nombre del .nsis.7z>`. Esa URL exacta
tiene que servir ese archivo exacto.

Si te equivocas, el build sale perfecto y **fallan todas las instalaciones**,
semanas después y sin nada a lo que recurrir. Por eso `build.ps1` se niega a
construir mientras la URL siga siendo el placeholder `REPLACE-ME`, e imprime
al final la URL completa que quedó grabada. Compruébala antes de publicar.

El servidor debe permitir descargas de 2.2 GB por HTTPS con `Content-Length`
correcto y sin caducidad de enlace.

### Si el build falla en `Cannot create symbolic link`

Síntoma, al llegar a electron-builder:

```
• signing with signtool.exe  path=release\win-unpacked\resources\motor\motor.exe
• downloading  url=...winCodeSign-2.6.0.7z
⨯ cannot execute  cause=exit status 2
  errorOut=ERROR: Cannot create symbolic link : A required privilege is not
  held by the client. : ...\winCodeSign\...\darwin\10.12\lib\libcrypto.dylib
```

**Qué pasa.** electron-builder intenta firmar cada `.exe` que va en
`extraResources` (`motor.exe`, `ffmpeg.exe`, `ffprobe.exe`, y un `protoc.exe`
que arrastra PyTorch). Para eso resuelve la ruta de su paquete `winCodeSign`
**antes** de comprobar si hay certificado, así que lo descarga aunque el
build sea sin firmar. Ese paquete es un `.7z` que contiene **symlinks de
macOS** (`libcrypto.dylib`, `libssl.dylib`). Crear un symlink en Windows
exige el privilegio `SeCreateSymbolicLinkPrivilege`, que un usuario normal no
tiene si el Modo de desarrollador está apagado, y la extracción falla.

Los dos `.dylib` son de macOS y aquí no sirven para nada.

**Cómo está resuelto.** `scripts\build.ps1` (función
`Initialize-WinCodeSignCache`) descarga el `.7z` y lo extrae él mismo
**saltándose el árbol `darwin`**, directamente en el directorio donde
electron-builder busca:

```
%LOCALAPPDATA%\electron-builder\Cache\winCodeSign\winCodeSign-2.6.0\
```

Al encontrarlo cacheado, electron-builder no descarga ni extrae nada y el
build sigue. Si la función falla, avisa y continúa: se vuelve al
comportamiento de antes, con un error claro.

Esto se arregla en el script y no en la configuración a propósito: es un
problema del entorno, no del producto, y así la firma seguirá funcionando el
día que se active sin tener que deshacer nada.

**La versión está fijada** a `winCodeSign-2.6.0`, que es la que pide
electron-builder 26.4.0. Si se actualiza electron-builder y pasa a pedir otra,
la función deja de coincidir, electron-builder intenta bajarla por su cuenta y
el error vuelve. En ese caso: actualizar `$version` en `build.ps1`, o bien

1. Activar **Modo de desarrollador** en Windows (Configuración → Sistema →
   Para programadores → Modo de desarrollador). Concede el privilegio al
   usuario y no hace falta nada más.
2. O correr el build **una vez** en un PowerShell como administrador.

Para comprobar si el privilegio está concedido:

```powershell
$t = Join-Path $env:TEMP 'symtest'; New-Item -ItemType Directory $t -Force | Out-Null
Set-Content (Join-Path $t 'real.txt') 'x'
New-Item -ItemType SymbolicLink -Path (Join-Path $t 'link.txt') -Target (Join-Path $t 'real.txt')
Remove-Item -Recurse -Force $t
```

---

## 2. Tamaño

Medido sobre el venv actual (torch 2.8.0+cu128, Python 3.12):

| Componente | Tamaño sin comprimir |
|---|---|
| DLL de PyTorch + CUDA (`torch/lib`, 37 archivos) | **4.17 GB** |
| ffmpeg.exe + ffprobe.exe | 198 MB |
| Todo lo demás (Python, demucs, numpy, Electron) | ~250 MB |

Las cinco DLL más grandes:

```
981 MB  torch_cuda.dll
643 MB  cublasLt64_12.dll
490 MB  cudnn_engines_precompiled64_9.dll
362 MB  cusparse64_12.dll
269 MB  cudnn_adv64_9.dll
```

Medido en el build 1.1.0: `win-unpacked` 4.81 GB → paquete comprimido
2.21 GB. Las DLL de CUDA bajan más o menos a la mitad con LZMA.

| Versión | Fecha | `dist\motor` | `win-unpacked` | Paquete `.nsis.7z` |
|---|---|---|---|---|
| 1.1.0 | 2026-09-20 | 4.31 GB | 4.81 GB | 2.21 GB |

### Palancas de tamaño que NO se aplicaron en v1

Con `nsis-web` ya no hay techo, así que esto dejó de ser urgente. Sigue
mereciendo la pena por el ancho de banda y por la paciencia del cliente:

- **ffmpeg más pequeño.** La palanca más fácil con diferencia. Los binarios
  actuales son builds completos de 99 MB cada uno. Uno compilado solo con lo
  que usamos (pcm, flac, lame, aac, y los demuxers de mp4/mkv/mov) baja de
  20 MB. **~150 MB, riesgo bajo**: es cuestión de encontrar el build.
- **Podar DLL de CUDA que no son dependencias de carga.** Verificado con
  `pefile` sobre la tabla de imports de `torch_cuda.dll`: `nvrtc` ×2 y
  `nvjitlink` (240 MB, JIT para `torch.compile`, que Demucs no usa),
  `curand` (69 MB) y `cusolverMg` (150 MB) no aparecen como imports de carga.
  Con más riesgo, `cudnn_engines_precompiled` (490 MB) y `cudnn_adv`
  (269 MB), que los carga el despachador de cuDNN en tiempo de ejecución.
  **~1.2 GB en juego**, pero cada recorte hay que verificarlo separando de
  verdad en GPU, no solo arrancando el motor: un kernel que falta puede no
  dar la cara hasta cierto tamaño de segmento.

Lo que **no** se puede tocar, porque son imports de carga de
`torch_cuda.dll` y sin ellos el DLL no carga: `cublasLt` (643 MB),
`cusparse` (362 MB), `cufft` (263 MB), `cusolver` (215 MB), `cublas`
(108 MB), más `torch_cuda` (981 MB) y `torch_cpu` (244 MB). **2.8 GB
intocables.**

---

## 3. SmartScreen: qué verá el cliente

**v1 se publica SIN FIRMAR, a propósito.** Eso significa que Windows va a
interponer una pantalla azul la primera vez que alguien ejecute el
instalador. No es un error ni un falso positivo de antivirus: es el
comportamiento normal de Windows con cualquier ejecutable sin certificado de
firma de código y sin reputación acumulada.

Lo que ve el cliente: una ventana azul, **"Windows protegió su PC"**
(*"Windows protected your PC"*), con un único botón visible, **No ejecutar**.

Hay que decírselo **antes** de que se lo encuentre. Si se lo encuentra sin
aviso, la mitad de la gente cancela y pide el reembolso.

### Texto para la página de descarga

**Español:**

> **Al instalar, Windows mostrará un aviso. Es normal.**
>
> La primera vez que ejecutes el instalador, Windows mostrará una pantalla
> azul que dice *"Windows protegió su PC"*. Aparece porque Polígono AI Hub
> es una aplicación nueva y todavía no tiene reputación acumulada en
> Microsoft SmartScreen, no porque el archivo tenga ningún problema.
>
> Para continuar:
>
> 1. Haz clic en **Más información**.
> 2. Haz clic en **Ejecutar de todas formas**.
>
> El instalador te pedirá permisos de administrador, porque instala en
> `C:\Program Files`.
>
> **El instalador descarga unos 2,2 GB** durante la instalación, así que
> necesitas conexión mientras se instala. La primera vez que separes un
> archivo se descargará además el modelo de IA (hasta 1 GB); después de eso,
> la aplicación funciona sin internet.

**English:**

> **Windows will show a warning when you install. This is expected.**
>
> The first time you run the installer, Windows shows a blue screen saying
> *"Windows protected your PC"*. It appears because Polígono AI Hub is a new
> application that has not yet built up a reputation with Microsoft
> SmartScreen — not because there is anything wrong with the file.
>
> To continue:
>
> 1. Click **More info**.
> 2. Click **Run anyway**.
>
> The installer will ask for administrator permission, because it installs
> into `C:\Program Files`.
>
> **The installer downloads about 2.2 GB** while it runs, so you need a
> connection during installation. The first time you separate a file it will
> also download the AI model (up to 1 GB); after that the app works offline.

Poner ese bloque **en la propia página de descarga**, junto al botón, no
escondido en un FAQ. Añadir una captura de la pantalla azul con el enlace
"Más información" señalado ayuda más que el texto.

### Lo que NO hay que decir

- No digas "desactiva el antivirus". Ni una vez. Es la frase que usa el
  malware y destruye la confianza.
- No digas "es un falso positivo". SmartScreen no está diciendo que el
  archivo sea malicioso; está diciendo que no lo conoce.
- Publica el hash SHA-256 del instalador junto al enlace. Cuesta una línea y
  da a un cliente técnico una forma de verificar la descarga:
  ```powershell
  Get-FileHash .\release\Poligono-AI-Hub-1.0.2-Setup.exe -Algorithm SHA256
  ```

---

## 4. Firmar más adelante

Firmar hace desaparecer la pantalla azul (con un certificado EV, de
inmediato; con uno normal o con Azure Trusted Signing, tras acumular algo de
reputación de descargas).

La configuración ya está escrita y verificada contra app-builder-lib 26.4.0.
Está en `package.json`, en la clave de primer nivel
**`windowsSigningTemplate`**, que electron-builder no lee.

No se puede dejar comentada dentro de `build`: el esquema de electron-builder
usa `additionalProperties: false` en la raíz y en `win`, así que cualquier
clave extra (incluida `"//"`) hace fallar la validación del build. De ahí que
viva fuera.

Para activarla:

1. Mueve el objeto `azureSignOptions` de `windowsSigningTemplate` a
   `build.win` en `package.json`.
2. Rellena `codeSigningAccountName` y `certificateProfileName` con los
   valores de la cuenta de Azure Trusted Signing, y comprueba que
   `endpoint` corresponde a su región.
3. `publisherName` debe coincidir **exactamente** con el nombre del
   certificado.
4. En la máquina de build, exporta `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` y
   `AZURE_CLIENT_SECRET`.
5. Vuelve a correr `.\scripts\build.ps1`.

Azure Trusted Signing es la opción barata (suscripción mensual, sin token
físico) y requiere verificar la identidad de la organización. Un certificado
OV/EV tradicional es la alternativa; en ese caso la configuración va en
`win.signtoolOptions` en vez de `win.azureSignOptions` (las dos son
excluyentes).

---

## 5. Modelos

**El instalador NO lleva los pesos.** Se descargan en el primer uso, con
barra de progreso en la fila del trabajo (implementado en la Fase 4,
`python/engine/models.py`).

| Modelo | Checkpoints | Tamaño aprox. |
|---|---|---|
| `htdemucs` (preset *fast*) | 1 | ~80 MB |
| `htdemucs_ft` (presets *hq* y *ultra*, por defecto) | 4 | ~1 GB |
| `mdx_extra` | 4 | ~1 GB |

Caen en el caché de `torch.hub`, es decir
`C:\Users\<usuario>\.cache\torch\hub\checkpoints\`. Es **por usuario** y
**sobrevive a desinstalar y reinstalar la app**: quien ya los tenga no los
vuelve a descargar.

Se verifica el SHA-256 que va en el nombre del archivo, y una descarga
interrumpida no deja un checkpoint corrupto (se escribe a un `.part` y solo
entonces se mueve).

### Paquete offline (futuro, no empaquetado)

Para estudios sin internet en la sala de máquinas, o para evitar 1 GB de
descarga en el primer arranque, se puede hacer una variante "offline" del
instalador:

1. Copiar los `.th` de `~/.cache/torch/hub/checkpoints/` a
   `resources/models/`.
2. Volver a añadir a `extraResources` en `package.json`:
   ```json
   { "from": "resources/models", "to": "models", "filter": ["**/*"] }
   ```
3. Hacer que el motor apunte `TORCH_HOME` a `process.resourcesPath/models`,
   o copiar los checkpoints al caché del usuario en el primer arranque.

**Nada de esto está implementado.** El paso 3 es el que tiene trabajo real:
hoy nadie lee `resources/models`. La entrada de `extraResources` que había en
`package.json` apuntando ahí se quitó justamente porque no la leía nadie y
solo generaba un warning en cada build.

---

## 6. Auto-update (futuro)

`build.publish` está en `null` a propósito: v1 no se actualiza sola. El
cliente descarga el instalador nuevo y lo ejecuta encima.

Cuando se quiera: `electron-updater` contra un servidor estático en el
hosting del estudio (`publish: { provider: "generic", url: "..." }`).
Dos advertencias antes de meterse:

- Con ~2 GB por versión, un diferencial importa. `nsis-web` + updates
  diferenciales, o se les hace pagar la descarga entera cada vez.
- `verifyUpdateCodeSignature` está en `true` por defecto y una app sin firmar
  no pasa esa verificación. Auto-update y firma van juntos; no tiene sentido
  hacer lo primero antes que lo segundo.

---

## 7. Checklist de release

- [ ] `python -m pytest tests -q` en verde.
- [ ] `npm run test:js` en verde.
- [ ] Subir la versión en `package.json`.
- [ ] **`build.nsisWeb.appPackageUrl` apunta a la carpeta real de descargas**,
      con barra final. El build se niega a correr si sigue el placeholder.
- [ ] `.\scripts\build.ps1 -Clean` completo, y que el smoke test diga
      `motor.exe answered pong`.
- [ ] **Subir el `.nsis.7z` a la URL de descargas ANTES de publicar el
      `.exe`.** Un instalador cuyo paquete no está todavía en su sitio falla
      en cada intento, sin diagnóstico útil para el cliente.
- [ ] Abrir `<appPackageUrl><nombre del .nsis.7z>` en un navegador desde otra
      red y comprobar que descarga los 2.2 GB enteros.
- [ ] Instalar en una **máquina limpia sin Python**: procesar un archivo en
      GPU y otro en CPU (Configuración → Dispositivo → Solo CPU).
- [ ] Instalar en una ruta con acentos y espacios
      (`C:\Program Files\Polígono AI Hub`) y procesar un archivo cuyo nombre
      también los lleve.
- [ ] Comprobar en el log qué dispositivo eligió y por qué
      (`GPU check: usable — ...`).
- [ ] Anotar el tamaño del instalador en la tabla de la sección 2.
- [ ] Publicar el SHA-256 junto al enlace de descarga.
- [ ] Que el aviso de SmartScreen esté en la página, junto al botón.
