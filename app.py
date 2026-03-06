import requests
import re
import json
import time
import threading
import os
from flask import Flask, render_template, jsonify, request, Response, stream_with_context

app = Flask(__name__)

# ══════════════════════════════════════════
#  CONFIGURACIÓN 10MA COMPAÑÍA (OFICIAL)
# ══════════════════════════════════════════
ID_PROCE         = '00000000aa450ce4'
CUARTEL_LAT      = -33.45798
CUARTEL_LON      = -70.641

cache = {
    'emergencia': None,
    'personal':   None,
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
#  FILTRO CENTRAL 10MA
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
            
            if any(n.endswith('10') for n in nombres_carros):
                lat = float(info.get('lat', CUARTEL_LAT)) 
                lon = float(info.get('lon', CUARTEL_LON)) 
                
                # Búsqueda implacable de la clave
                emerg = info.get('emergency', {})
                codigo = emerg.get('voceo_clave', emerg.get('voceo clave', emerg.get('clave', '10-0')))
                
                d1 = info.get('streetl', info.get('street1', ''))
                d2 = info.get('street2', '')
                direccion = f"{d1} / {d2}".strip(" / ")
                
                memoria.update({
                    'activa': True, 'codigo': codigo, 'direccion': direccion,
                    'unidades': ", ".join(nombres_carros), 'lat': lat, 'lon': lon
                })
                return memoria

        memoria['activa'] = False
        return memoria
    except Exception as e:
        memoria['activa'] = False
        return memoria

# ══════════════════════════════════════════
#  FETCH DATOS ICBS
# ══════════════════════════════════════════
def _fetch_emergencia():
    try:
        main_page = requests.get(f"https://icbs.cl/cuartel/index2.php?id_proce={ID_PROCE}", timeout=10).text
        m = re.search(r'time=(\d+)&hash=([a-fA-F0-9]+)', main_page)
        ft = m.group(1) if m else "1772758699"
        fh = m.group(2) if m else "141603737e80d51bb86cecd44d3ebd9c"

        datos = requests.get(f"https://icbs.cl/cuartel/datos.php?id_proce={ID_PROCE}&time={ft}&hash={fh}", timeout=10).json()
        datos['despacho_oficial'] = chequear_central()
        return datos
    except Exception as e:
        return None

# ══════════════════════════════════════════
#  RUTAS FLASK (Rutas Simples)
# ══════════════════════════════════════════
@app.route('/')
def home():
    return render_template('index.html')

@app.route('/api/personal')
def api_personal():
    if cache['personal']: return jsonify(cache['personal'])
    try:
        data = requests.get(f"https://icbs.cl/cuartel/com.php?id_proce={ID_PROCE}&traer=1", timeout=10).json()
        cache['personal'] = data
        return jsonify(data)
    except: return jsonify({"error": "No disponible"}), 503

@app.route('/api/emergencia')
def api_emergencia():
    if cache['emergencia']: return jsonify(cache['emergencia'])
    data = _fetch_emergencia()
    return jsonify(data) if data else (jsonify({"error": "No disponible"}), 503)

@app.route('/api/clima')
def api_clima():
    try: return jsonify(requests.get('https://icbs.cl/cuartel/clima.php', timeout=10).json())
    except: return jsonify({}), 503

@app.route('/api/cambiar_estado')
def api_cambiar_estado():
    ib, ipe, est = request.args.get('id_bombero', ''), request.args.get('id_personas_extra', ''), request.args.get('estado', '')
    requests.get(f"https://icbs.cl/cuartel/com.php?id_proce={ID_PROCE}&id_bombero={ib}&id_personas_extra={ipe}&estado={est}")
    return jsonify({"status": "ok"})

@app.route('/api/stream')
def stream():
    cola = []
    with clientes_lock: clientes_sse.append(cola)
    def generar():
        yield ": connected\n\n"
        while True:
            if cola: yield cola.pop(0)
            else: time.sleep(1)
    return Response(stream_with_context(generar()), mimetype='text/event-stream')

# ══════════════════════════════════════════
#  MOTORES INDEPENDIENTES
# ══════════════════════════════════════════
def _hash(obj): return json.dumps(obj, sort_keys=True, ensure_ascii=False)

def vigilante_json_central():
    ultimo_hash = ""
    while True:
        try:
            estado_central = chequear_central()
            nuevo_hash = _hash(estado_central)
            if nuevo_hash != ultimo_hash:
                ultimo_hash = nuevo_hash
                if cache.get('emergencia'):
                    cache['emergencia']['despacho_oficial'] = estado_central
                    msg_e = f"event: emergencia\ndata: {json.dumps(cache['emergencia'], ensure_ascii=False)}\n\n"
                    with clientes_lock:
                        for q in clientes_sse: q.append(msg_e)
        except: pass
        time.sleep(0.5)

def vigilante_icbs():
    ultimo_hash_p = ""
    ultimo_hash_e = ""
    while True:
        try:
            data_p = requests.get(f"https://icbs.cl/cuartel/com.php?id_proce={ID_PROCE}&traer=1", timeout=10).json()
            if _hash(data_p) != ultimo_hash_p:
                ultimo_hash_p = _hash(data_p)
                cache['personal'] = data_p
                msg_p = f"event: personal\ndata: {json.dumps(data_p, ensure_ascii=False)}\n\n"
                with clientes_lock:
                    for q in clientes_sse: q.append(msg_p)
        except: pass
        
        try:
            nuevo_emerg = _fetch_emergencia()
            if nuevo_emerg:
                if cache.get('emergencia') and 'despacho_oficial' in cache['emergencia']:
                    nuevo_emerg['despacho_oficial'] = cache['emergencia']['despacho_oficial']
                    
                if _hash(nuevo_emerg) != ultimo_hash_e:
                    ultimo_hash_e = _hash(nuevo_emerg)
                    cache['emergencia'] = nuevo_emerg
                    msg_e = f"event: emergencia\ndata: {json.dumps(nuevo_emerg, ensure_ascii=False)}\n\n"
                    with clientes_lock:
                        for q in clientes_sse: q.append(msg_e)
        except: pass
        time.sleep(8)

threading.Thread(target=vigilante_json_central, daemon=True).start()
threading.Thread(target=vigilante_icbs, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)