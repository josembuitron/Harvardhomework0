@echo off
REM ============================================================
REM  Optimizador de Work Areas - RouteSmart DRO
REM  Uso:
REM    - Doble clic (busca stop_information.csv en esta carpeta
REM      o en tu carpeta Descargas), o
REM    - Arrastra tu CSV sobre este archivo .bat
REM  Geocodificacion PRECISA con Google (opcional):
REM    set GOOGLE_MAPS_API_KEY=TU_KEY   antes de ejecutar.
REM    Sin key usa el geocoder del Census (gratis).
REM ============================================================
setlocal
cd /d "%~dp0"

echo.
echo === 1/2  Instalando dependencias de Python (una sola vez) ===
python -m pip install --quiet --upgrade pip
python -m pip install --quiet pandas numpy scikit-learn scipy folium openpyxl matplotlib requests
if errorlevel 1 (
  echo.
  echo ERROR: No se pudo ejecutar Python/pip. Instala Python 3.11+ desde
  echo https://www.python.org/downloads/  ^(marca "Add Python to PATH"^).
  pause
  exit /b 1
)

set "CSV=%~1"
if "%CSV%"=="" set "CSV=stop_information.csv"
if not exist "%CSV%" set "CSV=%USERPROFILE%\Downloads\stop_information.csv"

if not exist "%CSV%" (
  echo.
  echo No encontre el CSV. Arrastra tu archivo stop_information.csv sobre este .bat
  echo o colocalo en esta misma carpeta.
  pause
  exit /b 1
)

echo.
echo === 2/2  Optimizando con: "%CSV%" ===
python optimize_wa.py --input "%CSV%" --outdir salida
echo.
echo Listo. Abre la carpeta "salida":
echo   - propuesta_reasignacion_WA.xlsx
echo   - mapa_WA.html   ^(abrelo en tu navegador^)
echo   - mapa_WA.png
echo.
pause
