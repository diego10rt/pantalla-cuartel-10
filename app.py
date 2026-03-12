import requests
import json
import time
import threading
import os
from datetime import datetime
from flask import Flask, render_template, jsonify, request, Response, stream_with_context

app = Flask(__name__)

# ══════════════════════════════════════════
#  CONFIGURACIÓN 10MA COMPAÑÍA (PRUEBAS KOYEB)
# ══════════════════════════════════════════
ID_PROCE         = '00000000aa450ce4'
CUARTEL_LAT      = -33.45798
CUARTEL_LON      = -70.641

cache = {
    'emergencia': {
        'despacho_oficial': {
            'activa': False, 'codigo': '10-0', 'direccion': 'CUARTEL 10MA CIA',
            'unidades': '', 'lat': CUARTEL_LAT, 'lon': CUARTEL_LON
        },
        'carros': [],
        'llamados': [],
        'manual': []
    },
    'personal': None
}

clientes_sse  = []
clientes_lock = threading.Lock()

def _hash(obj): return json.dumps(obj, sort_keys=True, ensure_ascii=False)

# ══════════════════════════════════════════
#  MÓDULO 1: ESTADO CENTRAL Y CARROS (RÁPIDO)
# ══════════════════════════════════════════
def chequear_central():
    memoria = cache['emergencia']['despacho_oficial'].copy()
    try:
        r = requests.get('http://floppi4.floppi.one:5000/activos', timeout=5)
        for item in r.json().get('items', []):
            info = item.get('json', {})
            nombres = [v.get('name', '').upper() for v in info.get('vehicles', [])]
            if any(n.endswith('10') for n in nombres):
                emerg = info.get('emergency', {})
                codigo = emerg.get('voceo_clave', emerg.get('voceo clave', emerg.get('clave', '10-0')))
                direccion = f"{info.get('street1', '')} / {info.get('street2', '')}".strip(" / ")
                memoria.update({
                    'activa': True, 'codigo': codigo, 'direccion': direccion,
                    'unidades': ", ".join(nombres),
                    'lat': float(info.get('lat', CUARTEL_LAT)),
                    'lon': float(info.get('lon', CUARTEL_LON))
                })
                return memoria
        memoria['activa'] = False
        return memoria
    except:
        memoria['activa'] = False
        return memoria

def procesar_carros():
    try:
        r = requests.get('https://icbs.pipa.one/api/carros', timeout=5).json()
        carros_10 = []
        for c in r:
            name = c.get('nombre', '').upper()
            if name.endswith('10'):
                color = c.get('color', 'red')
                est = "DISPONIBLE" if color == 'green' else ("EN LLAMADO" if color == 'yellow' else "FUERA DE SERVICIO")
                carros_10.append({
                    "nombre": name,
                    "estado_nombre": est,
                    "color_raw": color, # Clave para ocultar el GPS cuando da el 6-10
                    "conductor": c.get('conductor') or '',
                    "lat": float(c.get('lat', 0)),
                    "lon": float(c.get('lng', 0))
                })
        return sorted(carros_10, key=lambda x: x['nombre'])
    except: return cache['emergencia']['carros']

# ══════════════════════════════════════════
#  MÓDULO 2: HISTORIAL Y MÉTRICAS (INDEPENDIENTE)
# ══════════════════════════════════════════
def procesar_historico():
    try:
        r = requests.get('https://icbs.pipa.one/api/llamados/cerrados', timeout=8).json()
        llamados = []
        for item in r.get('items', [])[:20]: # Extraemos los últimos 20 llamados
            info = item.get('json', {})
            codigo = info.get('emergency', {}).get('voceo_clave', '10-0')
            direccion = f"{info.get('street1', '')} / {info.get('street2', '')}".strip(" / ")
            fecha_raw = info.get('dispatch_time', '')
            
            unidades = []
            tiempo_respuesta = None
            
            for v in info.get('vehicles', []):
                name = v.get('name', '')
                unidades.append(name)
                
                # CÁLCULO MILITAR DE TIEMPO DE RESPUESTA PARA LA 10MA
                if name.endswith('10') and not tiempo_respuesta:
                    t60 = next((l['execution_time'] for l in v.get('logs', []) if l['code'] == '6-0'), None)
                    t63 = next((l['execution_time'] for l in v.get('logs', []) if l['code'] == '6-3'), None)
                    if t60 and t63:
                        try:
                            f = '%Y-%m-%d %H:%M:%S'
                            delta = int((datetime.strptime(t63, f) - datetime.strptime(t60, f)).total_seconds())
                            if delta > 0:
                                m, s = divmod(delta, 60)
                                tiempo_respuesta = f"{m}m {s}s ({name})"
                        except: pass
                        
            # Si filtramos para mostrar solo donde fue la 10 (opcional):
            # if any(u.endswith('10') for u in unidades):
            llamados.append({
                "codigo": codigo, 
                "direccion": direccion,
                "fecha": fecha_raw.split(" ")[-1][:5] if fecha_raw else "", # Extrae solo "HH:MM"
                "unidades": ", ".join(unidades),
                "tiempo_respuesta": tiempo_respuesta
            })
        return llamados
    except: return cache['emergencia']['llamados']

# ══════════════════════════════════════════
#  RUTAS FLASK
# ══════════════════════════════════════════
@app.route('/')
def home(): return render_template('index.html')

@app.route('/api/personal')
def api_personal():
    if cache['personal']: return jsonify(cache['personal'])
    try:
        data = requests.get(f"https://icbs.cl/cuartel/com.php?id_proce={ID_PROCE}&traer=1", timeout=5).json()
        cache['personal'] = data
        return jsonify(data)
    except: return jsonify({"error": "No disponible"}), 503

@app.route('/api/emergencia')
def api_emergencia(): return jsonify(cache['emergencia'])

@app.route('/api/clima')
def api_clima():
    try: return jsonify(requests.get('https://icbs.cl/cuartel/clima.php', timeout=5).json())
    except: return jsonify({}), 503

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
def vigilante_json_central():
    ultimo_hash = ""
    while True:
        try:
            estado = chequear_central()
            carros = procesar_carros()
            cache['emergencia']['despacho_oficial'] = estado
            cache['emergencia']['carros'] = carros
            
            nuevo_hash = _hash({'d': estado, 'c': carros})
            if nuevo_hash != ultimo_hash:
                ultimo_hash = nuevo_hash
                msg = f"event: emergencia\ndata: {json.dumps(cache['emergencia'], ensure_ascii=False)}\n\n"
                with clientes_lock:
                    for q in clientes_sse: q.append(msg)
        except: pass
        time.sleep(1) # Leemos GPS cada 1 segundo exacto

def vigilante_icbs():
    ultimo_hash_p = ""
    ultimo_hash_h = ""
    while True:
        try:
            data_p = requests.get(f"https://icbs.cl/cuartel/com.php?id_proce={ID_PROCE}&traer=1", timeout=8).json()
            if _hash(data_p) != ultimo_hash_p:
                ultimo_hash_p = _hash(data_p)
                cache['personal'] = data_p
                msg = f"event: personal\ndata: {json.dumps(data_p, ensure_ascii=False)}\n\n"
                with clientes_lock:
                    for q in clientes_sse: q.append(msg)
        except: pass
        
        try:
            llamados = procesar_historico()
            if _hash(llamados) != ultimo_hash_h:
                ultimo_hash_h = _hash(llamados)
                cache['emergencia']['llamados'] = llamados
                msg = f"event: emergencia\ndata: {json.dumps(cache['emergencia'], ensure_ascii=False)}\n\n"
                with clientes_lock:
                    for q in clientes_sse: q.append(msg)
        except: pass
        time.sleep(8)

threading.Thread(target=vigilante_json_central, daemon=True).start()
threading.Thread(target=vigilante_icbs, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)
