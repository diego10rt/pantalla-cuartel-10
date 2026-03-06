import requests
import re
import json
import time
import threading
from flask import Flask, render_template, jsonify, request, Response, stream_with_context

app = Flask(__name__)

# ══════════════════════════════════════════
#  CONFIGURACIÓN 10MA COMPAÑÍA (OFICIAL)
# ══════════════════════════════════════════
ID_PROCE         = '00000000aa450ce4'  # ¡ID Corregido!
CUARTEL_LAT      = -33.45798
CUARTEL_LON      = -70.641

# ══════════════════════════════════════════
#  CACHÉ GLOBAL & MEMORIA FOTOGRÁFICA
# ══════════════════════════════════════════
cache = {
    'emergencia': None,
    'personal':   None,
    'clima':      None,
    'clima_ts':   0,
    'memoria_despacho': {
        'activa': False,
        'codigo': '10-0',
        'direccion': 'CUARTEL 10MA CIA',
        'unidades': '',
        'lat': CUARTEL_LAT,
        'lon': CUARTEL_LON
    }
}

clientes_sse  = []
clientes_lock = threading.Lock()

# ══════════════════════════════════════════
#  MOTOR CENTRAL: MODO FRANCOTIRADOR
# ══════════════════════════════════════════
def chequear_central():
    memoria = cache['memoria_despacho']
    
    try:
        r = requests.get('http://floppi4.floppi.one:5000/activos', timeout=8)
        data = r.json()
        
        for item in data.get('items', []):
            info = item.get('json', {})
            vehiculos = info.get('vehicles', [])
            
            nombres_carros = [v.get('name', '').upper() for v in vehiculos]
            
            # FILTRO: ¿Hay alguna máquina que termine en "10"?
            if any(n.endswith('10') for n in nombres_carros):
                lat = float(info.get('lat', CUARTEL_LAT))
                lon = float(info.get('lon', CUARTEL_LON))
                codigo = info.get('emergency', {}).get('voceo clave', '10-0')
                
                d1 = info.get('street1', info.get('streetl', ''))
                d2 = info.get('street2', '')
                direccion = f"{d1} y {d2}".strip(" y ")
                
                memoria.update({
                    'activa': True,
                    'codigo': codigo,
                    'direccion': direccion,
                    'unidades': ", ".join(nombres_carros),
                    'lat': lat,
                    'lon': lon
                })
                print(f"[Central] ✓ Despacho 10ma activo: {codigo} - {direccion}")
                return memoria

        memoria['activa'] = False
        return memoria

    except Exception as e:
        print(f"[Central] Error de lectura: {e}")
        memoria['activa'] = False
        return memoria

# ══════════════════════════════════════════
#  FETCH EMERGENCIA COMPLETO
# ══════════════════════════════════════════
def _fetch_emergencia():
    try:
        main_page = requests.get(
            f"https://icbs.cl/cuartel/index2.php?id_proce={ID_PROCE}",
            timeout=10,
            headers={'User-Agent': 'Mozilla/5.0'}
        ).text
        
        # Buscamos las llaves de seguridad exactas de tu HTML
        m = re.search(r'time=(\d+)&hash=([a-fA-F0-9]+)', main_page)
        if m:
            ft = m.group(1)
            fh = m.group(2)
        else:
            print("[Fetch] Advertencia: Usando llaves de respaldo para la 10ma.")
            ft = "1772758699"
            fh = "141603737e80d51bb86cecd44d3ebd9c"

        datos = requests.get(
            f"https://icbs.cl/cuartel/datos.php?id_proce={ID_PROCE}&time={ft}&hash={fh}",
            timeout=10
        ).json()

        datos['despacho_oficial'] = chequear_central()
        return datos

    except Exception as e:
        print(f"[Fetch] Error general: {e}")
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
    print("[Vigilante] Iniciado con conexión a API Central (Filtro 10ma activo).")
    while True:
        try:
            data = requests.get(
                f"https://icbs.cl/cuartel/com.php?id_proce={ID_PROCE}&traer=1",
                timeout=10).json()
            if _hash(data) != _hash(cache.get('personal')):
                cache['personal'] = data
                notificar_clientes('personal', data)
        except Exception as e:
            pass

        try:
            nuevo = _fetch_emergencia()
            if nuevo and _hash(nuevo) != _hash(cache.get('emergencia')):
                cache['emergencia'] = nuevo
                notificar_clientes('emergencia', nuevo)
        except Exception as e:
            pass

        time.sleep(8)

threading.Thread(target=vigilante, daemon=True).start()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
