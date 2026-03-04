import requests
import re
import json
import time
import threading
from flask import Flask, render_template, jsonify, request, Response, stream_with_context

app = Flask(__name__)

# ══════════════════════════════════════════
#  CONFIGURACIÓN
# ══════════════════════════════════════════
ID_PROCE       = '00000000aa450ce4'
UNIDAD_EMERGENCI = '17'                          # Número de unidad en emergenci.app
EMERGENCI_URL  = f'https://emergenci.app/screen/{UNIDAD_EMERGENCI}'
CUARTEL_LAT    = -33.45798
CUARTEL_LON    = -70.641

# ══════════════════════════════════════════
#  CACHÉ GLOBAL
# ══════════════════════════════════════════
cache = {
    'emergencia': None,
    'personal':   None,
    'clima':      None,
    'clima_ts':   0,
    'geo_cache':  {},   # texto_dirección → {lat, lon}
}

clientes_sse  = []
clientes_lock = threading.Lock()

# ══════════════════════════════════════════
#  FUENTE 1: EMERGENCI.APP
#  Lee directamente los atributos data- del mapa
#  Estos son coordenadas GPS exactas del CBS
# ══════════════════════════════════════════
def coords_desde_emergenci():
    """
    Retorna {'lat': float, 'lon': float} con las coordenadas exactas
    de la emergencia activa leyendo emergenci.app/screen/{unidad}
    Retorna None si falla o no hay emergencia activa.
    """
    try:
        r = requests.get(
            EMERGENCI_URL,
            timeout=8,
            headers={'User-Agent': 'Mozilla/5.0 (compatible; Bomberos10cia/1.0)'}
        )
        html = r.text

        lat_m = re.search(r'data-emergencia-lat=["\']?([^"\'>\s]+)', html)
        lon_m = re.search(r'data-emergencia-lon=["\']?([^"\'>\s]+)', html)

        if not lat_m or not lon_m:
            print("[Emergenci] No se encontraron coordenadas en el HTML")
            return None

        lat = float(lat_m.group(1))
        lon = float(lon_m.group(1))

        # Validar que no sean las coordenadas del cuartel (sin emergencia activa)
        dist = ((lat - CUARTEL_LAT)**2 + (lon - CUARTEL_LON)**2) ** 0.5
        if dist < 0.0005:   # ~55 metros
            print(f"[Emergenci] Coordenadas == cuartel, sin emergencia activa")
            return None

        print(f"[Emergenci] ✓ Coordenadas GPS exactas: {lat}, {lon}")
        return {'lat': lat, 'lon': lon}

    except Exception as e:
        print(f"[Emergenci] Error al leer emergenci.app: {e}")
        return None


# ══════════════════════════════════════════
#  FUENTE 2: GEOCODING (respaldo)
#  Solo se usa si emergenci.app falla
# ══════════════════════════════════════════
def extraer_direccion(texto):
    """Limpia el texto del llamado y deja solo la dirección."""
    if not texto:
        return ''
    # Quitar código: "10-3-5 " o "10-2 "
    t = re.sub(r'^[\d]+-[\d]+-[\d]+\s*', '', texto).strip()
    t = re.sub(r'^[\d]+-[\d]+\s*', '', t).strip()
    # Quitar unidades al final: " BX10, MX13, RX7"
    t = re.sub(r'\s+[A-Z]{1,3}\d+(?:,\s*[A-Z]{1,3}\d+)*\s*$', '', t).strip()
    return t


def geocodificar(direccion_raw):
    """Nominatim como respaldo. Resultado cacheado."""
    if not direccion_raw:
        return None

    dir_limpia = direccion_raw.replace('/', ' y ').strip()
    clave = dir_limpia.lower()

    if clave in cache['geo_cache']:
        return cache['geo_cache'][clave]

    query   = dir_limpia + ', Santiago, Chile'
    headers = {'User-Agent': 'Bomberos10cia/1.0 dashboard-interno'}
    try:
        r    = requests.get('https://nominatim.openstreetmap.org/search',
                            params={'format': 'json', 'q': query, 'limit': 1},
                            headers=headers, timeout=8)
        data = r.json()
        if data:
            res = {'lat': float(data[0]['lat']), 'lon': float(data[0]['lon'])}
            cache['geo_cache'][clave] = res
            print(f"[Geo] Respaldo OK: {dir_limpia} → {res}")
            return res
        print(f"[Geo] Sin resultado: {query}")
        return None
    except Exception as e:
        print(f"[Geo] Error: {e}")
        return None


# ══════════════════════════════════════════
#  FETCH EMERGENCIA COMPLETO
#  Intenta coordenadas en este orden:
#  1. emergenci.app  (GPS exacto del CBS)
#  2. Nominatim      (geocoding por texto)
#  3. None           (el frontend no mueve el mapa)
# ══════════════════════════════════════════
def _fetch_emergencia():
    try:
        # Hash fresco de SIAC
        main_page = requests.get(
            f"https://icbs.cl/cuartel/index2.php?id_proce={ID_PROCE}",
            timeout=10
        ).text
        m = re.search(r'time=(\d+)&hash=([a-fA-F0-9]+)', main_page)
        ft = m.group(1) if m else "1772539491"
        fh = m.group(2) if m else "8e047780fd8b4f351b7c9e0c03b9fa63"

        # Datos: carros, llamados, novedades
        datos = requests.get(
            f"https://icbs.cl/cuartel/datos.php?id_proce={ID_PROCE}&time={ft}&hash={fh}",
            timeout=10
        ).json()

        # ── FUENTE 1: emergenci.app ──────────────────
        coords = coords_desde_emergenci()

        # ── FUENTE 2: geocoding de respaldo ──────────
        if coords is None:
            llamados = datos.get('llamados') or []
            if llamados:
                texto     = llamados[0].get('texto', '')
                direccion = extraer_direccion(texto)
                if direccion:
                    coords = geocodificar(direccion)
                    if coords:
                        print(f"[Fetch] Usando geocoding de respaldo")

        datos['coordenadas_exactas'] = coords  # None si ambas fuentes fallaron
        return datos

    except Exception as e:
        print(f"[Fetch] Error: {e}")
        return None


# ══════════════════════════════════════════
#  NOTIFICACIÓN SSE
# ══════════════════════════════════════════
def notificar_clientes(evento, datos):
    msg = f"event: {evento}\ndata: {json.dumps(datos, ensure_ascii=False)}\n\n"
    with clientes_lock:
        muertos = []
        for q in clientes_sse:
            try:    q.append(msg)
            except: muertos.append(q)
        for q in muertos:
            clientes_sse.remove(q)


# ══════════════════════════════════════════
#  RUTAS FLASK
# ══════════════════════════════════════════
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/personal')
def api_personal():
    if cache['personal']:
        return jsonify(cache['personal'])
    try:
        data = requests.get(
            f"https://icbs.cl/cuartel/com.php?id_proce={ID_PROCE}&traer=1",
            timeout=10).json()
        cache['personal'] = data
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 503

@app.route('/api/emergencia')
def api_emergencia():
    if cache['emergencia']:
        return jsonify(cache['emergencia'])
    data = _fetch_emergencia()
    return jsonify(data) if data else (jsonify({"error": "No disponible"}), 503)

@app.route('/api/clima')
def api_clima():
    ahora = time.time()
    if cache['clima'] and (ahora - cache['clima_ts']) < 600:
        return jsonify(cache['clima'])
    try:
        data = requests.get('https://icbs.cl/cuartel/clima.php', timeout=10).json()
        cache['clima']    = data
        cache['clima_ts'] = ahora
        return jsonify(data)
    except Exception as e:
        return jsonify(cache['clima']) if cache['clima'] else (jsonify({"error": str(e)}), 503)

@app.route('/api/cambiar_estado')
def api_cambiar_estado():
    ib  = request.args.get('id_bombero', '')
    ipe = request.args.get('id_personas_extra', '')
    est = request.args.get('estado', '')
    try:
        requests.get(
            f"https://icbs.cl/cuartel/com.php?id_proce={ID_PROCE}"
            f"&id_bombero={ib}&id_personas_extra={ipe}&estado={est}",
            timeout=10)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 503

@app.route('/api/registro', methods=['POST'])
def api_registro():
    codigo = request.form.get('registro', '')
    try:
        r = requests.post(
            f"https://icbs.cl/cuartel/com.php?id_proce={ID_PROCE}",
            data={'registro': codigo}, timeout=10)
        try:    return jsonify(r.json())
        except: return jsonify({"msg": "OK"})
    except:
        return jsonify({"msg": "Error de conexión"})

@app.route('/api/stream')
def stream():
    cola = []
    with clientes_lock:
        clientes_sse.append(cola)
    if cache['emergencia']:
        cola.append(f"event: emergencia\ndata: {json.dumps(cache['emergencia'], ensure_ascii=False)}\n\n")
    if cache['personal']:
        cola.append(f"event: personal\ndata: {json.dumps(cache['personal'], ensure_ascii=False)}\n\n")

    def generar():
        yield ": connected\n\n"
        while True:
            if cola:
                yield cola.pop(0)
                time.sleep(0.05)
            else:
                yield ": heartbeat\n\n"
                time.sleep(25)

    return Response(stream_with_context(generar()),
                    mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache',
                             'X-Accel-Buffering': 'no',
                             'Connection': 'keep-alive'})


# ══════════════════════════════════════════
#  HILO VIGILANTE
# ══════════════════════════════════════════
def _hash(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)

def vigilante():
    print("[Vigilante] Iniciado.")
    while True:
        # Personal
        try:
            data = requests.get(
                f"https://icbs.cl/cuartel/com.php?id_proce={ID_PROCE}&traer=1",
                timeout=10).json()
            if _hash(data) != _hash(cache.get('personal')):
                cache['personal'] = data
                notificar_clientes('personal', data)
                print("[Vigilante] Personal → SSE")
        except Exception as e:
            print(f"[Vigilante] Error personal: {e}")

        # Emergencia
        try:
            nuevo = _fetch_emergencia()
            if nuevo and _hash(nuevo) != _hash(cache.get('emergencia')):
                cache['emergencia'] = nuevo
                notificar_clientes('emergencia', nuevo)
                src = "GPS Emergenci" if nuevo.get('coordenadas_exactas') else "sin coords"
                print(f"[Vigilante] Emergencia → SSE ({src})")
        except Exception as e:
            print(f"[Vigilante] Error emergencia: {e}")

        time.sleep(8)

threading.Thread(target=vigilante, daemon=True).start()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)