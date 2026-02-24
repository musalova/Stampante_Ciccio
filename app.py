from flask import Flask, render_template, request, jsonify, Response
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta
from io import BytesIO
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
import os
import threading
from leggi_fogli import get_client

app = Flask(__name__)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 3600

# --- CACHE IN MEMORY ---
_cache = {
    'anagrafica': [],
    'giacenze': [],
    'timestamp': None,
    'sheet_ref': None
}
_cache_lock = threading.Lock()
CACHE_TTL_SECONDS = 30  # Cache valida per 30 secondi

# Dimensioni etichetta comandiera cucina (58x40 mm)
LABEL_W = 58 * mm
LABEL_H = 40 * mm

def _is_truthy(val):
    s = str(val or '').strip().lower()
    return s in ('si', 'sì', 'yes', 'y', 'true', '1')

def _canon_categoria(cat_raw):
    c = str(cat_raw or '').strip().lower()
    return 'interno' if 'intern' in c else 'esterno'

def _parse_date_any(s):
    s = str(s or '').strip()
    for fmt in ('%Y-%m-%d', '%d/%m/%Y'):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return None


def _is_cache_valid():
    """Verifica se la cache è ancora valida."""
    if _cache['timestamp'] is None:
        return False
    elapsed = (datetime.now() - _cache['timestamp']).total_seconds()
    return elapsed < CACHE_TTL_SECONDS


def invalidate_cache():
    """Invalida la cache (chiamare dopo modifiche)."""
    with _cache_lock:
        _cache['timestamp'] = None


def _leggi_db_cached(force_refresh=False):
    """Legge dati da cache o da Google Sheets se cache scaduta."""
    with _cache_lock:
        if not force_refresh and _is_cache_valid():
            return _cache['anagrafica'], _cache['giacenze'], _cache['sheet_ref']
    
    # Leggi da Google Sheets
    try:
        sh = get_client().open("Database_Ciccio_Lumia")
        ws_anag = sh.worksheet("ANAGRAFICA")
        anagrafica = ws_anag.get_all_records()
        
        ws_giac = sh.worksheet("GIACENZE")
        righe_grezze = ws_giac.get_all_values()
        
        giacenze_pulite = []
        if len(righe_grezze) > 1:
            for i, r in enumerate(righe_grezze[1:], start=2): 
                if len(r) > 0 and r[0].strip():
                    try:
                        qta = int(r[1])
                    except (ValueError, IndexError, TypeError):
                        qta = 0
                    
                    if qta > 0:
                        giacenze_pulite.append({
                            'riga_id': i,
                            'Prodotto': r[0].strip(),
                            'Quantità_Attuale': qta,
                            'Lotto': r[4] if len(r) > 4 else "N/D",
                            'Scadenza': r[5] if len(r) > 5 else "N/D",
                            'Data_Inizio': r[6] if len(r) > 6 else "N/D"
                        })
        
        # Aggiorna cache
        with _cache_lock:
            _cache['anagrafica'] = anagrafica
            _cache['giacenze'] = giacenze_pulite
            _cache['timestamp'] = datetime.now()
            _cache['sheet_ref'] = sh
        
        return anagrafica, giacenze_pulite, sh
    except Exception as e:
        print(f"ERRORE CRITICO: {e}")
        # Se c'è errore, prova a usare cache vecchia
        with _cache_lock:
            if _cache['anagrafica']:
                return _cache['anagrafica'], _cache['giacenze'], _cache['sheet_ref']
        return [], [], None


# Mantieni compatibilità con codice esistente
def leggi_db():
    return _leggi_db_cached()


def _calcola_scadenza_da_anagrafica(p_info):
    """Calcola data scadenza da ANAGRAFICA: giorni conservazione da oggi."""
    for key in ('GIORNI_SCADENZA', 'GIORNI_CONSERVAZIONE', 'SCADENZA_GIORNI', 'CONSERVAZIONE', 'SHELF_LIFE', 'DURATA_GIORNI'):
        val = p_info.get(key)
        if val is not None and str(val).strip():
            try:
                giorni = int(float(str(val).replace(',', '.')))
                scad = datetime.now() + timedelta(days=giorni)
                return scad.strftime('%d/%m/%Y')
            except (ValueError, TypeError):
                pass
    return None


def _normalizza_lotto_esterno(lotto):
    """Assicura che il lotto esterno inizi con 'L'."""
    if not lotto or lotto.strip() == '':
        return lotto
    lotto = lotto.strip().upper()
    if not lotto.startswith('L'):
        lotto = 'L' + lotto
    return lotto


def _disegna_etichetta(c, nome, data_inizio, data_scadenza, lotto):
    """Disegna etichetta con contenuto distribuito su tutta l'altezza."""
    margin = 2 * mm
    w, h = LABEL_W, LABEL_H
    
    # Sfondo bianco
    c.setFillColorRGB(1, 1, 1)
    c.rect(0, 0, w, h, fill=1, stroke=0)
    
    # Logo a sinistra, centrato verticalmente
    logo_path = os.path.join(os.path.dirname(__file__), 'static', 'images', 'logo.jpg')
    logo_presente = False
    if os.path.exists(logo_path):
        try:
            logo_size = 18 * mm
            logo_x = margin + 1 * mm
            logo_y = (h - logo_size) / 2
            c.drawImage(logo_path, logo_x, logo_y, width=logo_size, height=logo_size, preserveAspectRatio=True)
            logo_presente = True
        except Exception as e:
            print(f"Errore caricamento logo: {e}")
    
    # Area testo: inizia dopo il logo
    if logo_presente:
        testo_x_start = margin + 20 * mm
    else:
        testo_x_start = margin + 2 * mm
    
    c.setFillColorRGB(0, 0, 0)
    
    # Layout con spaziatura aumentata per riempire l'altezza
    max_chars = 16
    nome_lungo = len(nome) > max_chars
    
    # Interlinea più ampia per distribuire verso il basso
    line_height = 9 * mm  # aumentato da 7mm
    y_pos = h - margin - 3 * mm  # inizia un po' più in alto
    
    if nome_lungo:
        # Nome lungo: spezza su 2 righe
        prod_font = 10
        c.setFont("Helvetica-Bold", prod_font)
        
        words = nome.split()
        line1 = ""
        line2 = ""
        current = ""
        
        for word in words:
            if len(current + word + " ") <= max_chars:
                current += word + " "
            else:
                if not line1:
                    line1 = current.strip()
                    current = word + " "
                else:
                    current += word + " "
        
        if not line1:
            line1 = current.strip()[:max_chars]
        else:
            line2 = current.strip()[:max_chars]
        
        if not line2 and len(nome) > max_chars and not line1:
            line1 = nome[:max_chars]
            line2 = nome[max_chars:max_chars*2].strip()
        
        if line1:
            c.drawString(testo_x_start, y_pos, line1)
            y_pos -= 6 * mm  # spazio ridotto per far salire seconda riga
        if line2:
            c.drawString(testo_x_start, y_pos, line2)
            y_pos -= 5 * mm  # ridotto per far risalire il lotto
        else:
            y_pos -= 7 * mm
    else:
        # Nome breve: 1 riga con font grande
        prod_font = 12
        c.setFont("Helvetica-Bold", prod_font)
        c.drawString(testo_x_start, y_pos, nome)
        y_pos -= line_height + 1 * mm  # spazio extra dopo nome
    
    # DATA e SCADENZA con interlinea ampia
    c.setFont("Helvetica", 9)
    c.drawString(testo_x_start, y_pos, f"Data: {data_inizio}")
    y_pos -= line_height - 1 * mm
    
    c.drawString(testo_x_start, y_pos, f"Scad: {data_scadenza}")
    y_pos -= line_height + 1 * mm  # spazio extra prima del lotto
    
    # LOTTO in fondo - font adattivo
    lotto_text = f"Lotto: {lotto}"
    lotto_font = 10
    if len(lotto_text) > 20:
        lotto_font = 9
    if len(lotto_text) > 24:
        lotto_font = 8
    
    c.setFont("Helvetica-Bold", lotto_font)
    c.drawString(testo_x_start, y_pos, lotto_text)


def genera_etichetta_pdf(nome, lotto, data_inizio=None, data_scadenza=None):
    """Crea un PDF con etichetta 58x40mm per stampante da cucina."""
    if data_inizio is None:
        data_inizio = datetime.now().strftime('%d/%m/%Y')
    if data_scadenza is None:
        data_scadenza = "N/D"
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=(LABEL_W, LABEL_H))
    c.setTitle(f"Etichetta - {nome}")
    _disegna_etichetta(c, nome, data_inizio, data_scadenza, lotto)
    c.showPage()
    c.save()
    buffer.seek(0)
    return buffer.getvalue()

def genera_pdf_multi(labels):
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=(LABEL_W, LABEL_H))
    c.setTitle("Linea Ciccio's")
    for lab in labels:
        qta = int(lab.get('qta', 1)) if lab.get('qta') else 1
        for _ in range(max(1, qta)):
            _disegna_etichetta(c, lab['nome'], lab['data_inizio'], lab['data_scadenza'], lab['lotto'])
            c.showPage()
    c.save()
    buffer.seek(0)
    return buffer.getvalue()

@app.route('/')
def index():
    anag, _, _ = _leggi_db_cached()
    prodotti = sorted(anag, key=lambda p: str(p.get('PRODOTTO', '')).strip().lower())
    return render_template('index.html', prodotti=prodotti)

@app.route('/magazzino')
def magazzino():
    _, giacenze, _ = _leggi_db_cached()
    agg = {}
    for g in giacenze:
        nome = g.get('Prodotto', '').strip()
        q = int(g.get('Quantità_Attuale', 0)) if g.get('Quantità_Attuale') else 0
        agg[nome] = agg.get(nome, 0) + q
    giac_list = [{'Prodotto': k, 'Quantità_Totale': v} for k, v in agg.items()]
    giac_list.sort(key=lambda x: x['Prodotto'].lower())
    anag, _, _ = _leggi_db_cached()
    nomi = sorted([str(p.get('PRODOTTO', '')).strip() for p in anag if str(p.get('PRODOTTO', '')).strip()])
    return render_template('magazzino.html', giacenze=giac_list, nomi_prodotti=nomi)

@app.route('/stampa_singolo', methods=['POST'])
def stampa_singolo():
    dati = request.json or {}
    nome = (dati.get('nome') or '').strip()
    try:
        qta = int(dati.get('quantita') or dati.get('qta') or 1)
    except Exception:
        qta = 1
    qta = max(1, min(qta, 500))
    if not nome:
        return jsonify({"status": "error", "message": "Nome prodotto mancante."}), 400
    anag, giac, sh = _leggi_db_cached(force_refresh=True)
    if not sh:
        return jsonify({"status": "error", "message": "Errore connessione database."}), 500
    p_info = next((p for p in anag if str(p.get('PRODOTTO', '')).strip().lower() == nome.lower()), None)
    if not p_info:
        return jsonify({"status": "error", "message": "Prodotto non trovato."}), 404
    cat = _canon_categoria(p_info.get('CATEGORIA', ''))
    sigla = p_info.get('SIGLA', 'XX')
    if cat == 'interno':
        lotto = f"L{datetime.now().strftime('%d%d%m%y')}{sigla}"
    else:
        lotto_raw = (dati.get('lotto') or '').strip()
        if lotto_raw:
            lotto = _normalizza_lotto_esterno(lotto_raw)
        else:
            candidati = [g for g in giac if str(g.get('Prodotto', '')).strip().lower() == nome.lower()]
            candidati.sort(key=lambda x: (_parse_date_any(x.get('Data_Inizio')) or datetime.min, x.get('riga_id', 0)), reverse=True)
            if candidati:
                lotto = _normalizza_lotto_esterno(str(candidati[0].get('Lotto', '')).strip())
                if not lotto:
                    return jsonify({"status": "error", "message": "Lotto esterno mancante. Inseriscilo."}), 400
            else:
                return jsonify({"status": "error", "message": "Lotto esterno mancante. Inseriscilo."}), 400
    data_inizio = datetime.now().strftime('%d/%m/%Y')
    data_scadenza = _calcola_scadenza_da_anagrafica(p_info) or "N/D"
    scad_sheet = datetime.now().strftime('%Y-%m-%d')
    if data_scadenza != "N/D":
        try:
            dt = datetime.strptime(data_scadenza, '%d/%m/%Y')
            scad_sheet = dt.strftime('%Y-%m-%d')
        except ValueError:
            pass
    ws = sh.worksheet("GIACENZE")
    mappa = {}
    for g in giac:
        key = (g['Prodotto'].strip().lower(), _normalizza_lotto_esterno(str(g.get('Lotto', '')).strip()))
        mappa[key] = (g['riga_id'], int(g.get('Quantità_Attuale', 0)))
    key = (nome.strip().lower(), lotto)
    try:
        if key in mappa:
            riga_id, q = mappa[key]
            ws.update_cell(riga_id, 2, q + qta)
        else:
            data_inizio_sheet = datetime.now().strftime('%Y-%m-%d')
            ws.append_row([nome, qta, 'busta', '', lotto, scad_sheet, data_inizio_sheet])
        invalidate_cache()
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    pdf = genera_pdf_multi([{'nome': nome, 'lotto': lotto, 'data_inizio': data_inizio, 'data_scadenza': data_scadenza, 'qta': qta}])
    return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": f"inline; filename=etichetta_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf", "Content-Length": str(len(pdf))})

@app.route('/prepara_stampa_linea')
def prepara_stampa_linea():
    anag, giac, _ = _leggi_db_cached(force_refresh=True)
    prodotti = []
    for p in anag:
        if _is_truthy(p.get('OBBLIGATORIO_GIORNALIERO', '')):
            nome = str(p.get('PRODOTTO', '')).strip()
            if not nome:
                continue
            cat = _canon_categoria(p.get('CATEGORIA', ''))
            qta = 1
            try:
                qta = int(p.get('Pezzi_in_Linea') or 1)
            except Exception:
                qta = 1
            sigla = p.get('SIGLA', 'XX')
            if cat == 'interno':
                lotto = f"L{datetime.now().strftime('%d%d%m%y')}{sigla}"
            else:
                candidati = [g for g in giac if str(g.get('Prodotto', '')).strip().lower() == nome.lower()]
                candidati.sort(key=lambda x: (_parse_date_any(x.get('Data_Inizio')) or datetime.min, x.get('riga_id', 0)), reverse=True)
                if candidati:
                    lotto = _normalizza_lotto_esterno(str(candidati[0].get('Lotto', '')).strip()) or ''
                else:
                    lotto = ''
            prodotti.append({'nome': nome, 'categoria': cat, 'lotto': lotto, 'qta': qta})
    return jsonify({"prodotti": prodotti})

@app.route('/stampa_linea_totale', methods=['POST'])
def stampa_linea_totale():
    dati = request.json or {}
    items = dati.get('items') or []
    if not items:
        return jsonify({"status": "error", "message": "Nessun prodotto da stampare."}), 400
    anag, giac, sh = _leggi_db_cached(force_refresh=True)
    if not sh:
        return jsonify({"status": "error", "message": "Errore connessione database."}), 500
    by_name = {str(p.get('PRODOTTO', '')).strip().lower(): p for p in anag}
    ws = sh.worksheet("GIACENZE")
    mappa = {}
    for g in giac:
        key = (g['Prodotto'].strip().lower(), _normalizza_lotto_esterno(str(g.get('Lotto', '')).strip()))
        mappa[key] = (g['riga_id'], int(g.get('Quantità_Attuale', 0)))
    labels = []
    for it in items:
        nome = (it.get('nome') or '').strip()
        if not nome:
            continue
        p_info = by_name.get(nome.lower())
        if not p_info:
            continue
        cat = _canon_categoria(p_info.get('CATEGORIA', ''))
        sigla = p_info.get('SIGLA', 'XX')
        if cat == 'interno':
            lotto = f"L{datetime.now().strftime('%d%d%m%y')}{sigla}"
        else:
            lotto_raw = (it.get('lotto') or '').strip()
            if lotto_raw:
                lotto = _normalizza_lotto_esterno(lotto_raw)
            else:
                candidati = [g for g in giac if str(g.get('Prodotto', '')).strip().lower() == nome.lower()]
                candidati.sort(key=lambda x: (_parse_date_any(x.get('Data_Inizio')) or datetime.min, x.get('riga_id', 0)), reverse=True)
                if candidati:
                    lotto = _normalizza_lotto_esterno(str(candidati[0].get('Lotto', '')).strip())
                    if not lotto:
                        return jsonify({"status": "error", "message": f"Manca lotto per {nome}."}), 400
                else:
                    return jsonify({"status": "error", "message": f"Manca lotto per {nome}."}), 400
        data_inizio = datetime.now().strftime('%d/%m/%Y')
        data_inizio_sheet = datetime.now().strftime('%Y-%m-%d')
        # Per stampa linea: scadenza giornaliera (oggi)
        data_scadenza = datetime.now().strftime('%d/%m/%Y')
        scad_sheet = datetime.now().strftime('%Y-%m-%d')
        try:
            qta = int(it.get('qta') or 1)
        except Exception:
            qta = 1
        qta = max(1, min(qta, 500))
        key = (nome.strip().lower(), lotto)
        if key in mappa:
            riga_id, q = mappa[key]
            try:
                ws.update_cell(riga_id, 2, q + qta)
                ws.update_cell(riga_id, 6, scad_sheet)
            except Exception as e:
                print(e)
        else:
            try:
                ws.append_row([nome, qta, 'busta', '', lotto, scad_sheet, data_inizio_sheet])
            except Exception as e:
                print(e)
        labels.append({'nome': nome, 'lotto': lotto, 'data_inizio': data_inizio, 'data_scadenza': data_scadenza, 'qta': qta})
    try:
        invalidate_cache()
    except Exception:
        pass
    pdf = genera_pdf_multi(labels)
    nome_file = f"linea_ciccio_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
    return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": f"inline; filename={nome_file}", "Content-Length": str(len(pdf))})

@app.route('/aggiungi_prodotto', methods=['POST'])
def aggiungi_prodotto():
    dati = request.json or {}
    nome = (dati.get('nome') or '').strip()
    try:
        qta = int(dati.get('qta') or 0)
    except Exception:
        qta = 0
    qta = max(0, min(qta, 500))
    if not nome or qta < 1:
        return jsonify({"status": "error", "message": "Dati non validi."}), 400
    anag, giac, sh = _leggi_db_cached(force_refresh=True)
    if not sh:
        return jsonify({"status": "error", "message": "Errore connessione database."}), 500
    p_info = next((p for p in anag if str(p.get('PRODOTTO', '')).strip().lower() == nome.lower()), None)
    cat = str(p_info.get('CATEGORIA', '')).strip().lower() if p_info else ''
    sigla = p_info.get('SIGLA', 'XX') if p_info else 'XX'
    if cat == 'interno':
        lotto = f"L{datetime.now().strftime('%d%d%m%y')}{sigla}"
    else:
        lotto = _normalizza_lotto_esterno((dati.get('lotto') or '').strip()) if dati.get('lotto') else ''
    data_scadenza = _calcola_scadenza_da_anagrafica(p_info) if p_info else None
    scad_sheet = datetime.now().strftime('%Y-%m-%d')
    if data_scadenza:
        try:
            dt = datetime.strptime(data_scadenza, '%d/%m/%Y')
            scad_sheet = dt.strftime('%Y-%m-%d')
        except Exception:
            pass
    ws = sh.worksheet("GIACENZE")
    mappa = {}
    for g in giac:
        key = (g['Prodotto'].strip().lower(), _normalizza_lotto_esterno(str(g.get('Lotto', '')).strip()))
        mappa[key] = (g['riga_id'], int(g.get('Quantità_Attuale', 0)))
    key = (nome.strip().lower(), lotto)
    try:
        if key in mappa:
            riga_id, q = mappa[key]
            ws.update_cell(riga_id, 2, q + qta)
        else:
            data_inizio_sheet = datetime.now().strftime('%Y-%m-%d')
            ws.append_row([nome, qta, 'busta', '', lotto, scad_sheet, data_inizio_sheet])
        invalidate_cache()
        return jsonify({"status": "success", "lotto": lotto})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/stampa_ristampa', methods=['POST'])
def stampa_ristampa():
    dati = request.json or {}
    nome = (dati.get('nome') or '').strip()
    qta = int(dati.get('qta') or 1)
    lotto_in = (dati.get('lotto') or '').strip()
    if not nome:
        return jsonify({"status": "error", "message": "Nome prodotto mancante."}), 400
    anag, giac, sh = _leggi_db_cached(force_refresh=True)
    p_info = next((p for p in anag if str(p.get('PRODOTTO', '')).strip().lower() == nome.lower()), None)
    if not p_info:
        return jsonify({"status": "error", "message": "Prodotto non trovato."}), 404
    cat = _canon_categoria(p_info.get('CATEGORIA', ''))
    sigla = p_info.get('SIGLA', 'XX')
    lotto = ''
    data_inizio = None
    data_scadenza = None
    if lotto_in:
        lotto = _normalizza_lotto_esterno(lotto_in)
    if not lotto:
        candidati = [g for g in giac if str(g.get('Prodotto', '')).strip().lower() == nome.lower()]
        candidati.sort(key=lambda x: (_parse_date_any(x.get('Data_Inizio')) or datetime.min, x.get('riga_id', 0)), reverse=True)
        if candidati:
            lotto = _normalizza_lotto_esterno(str(candidati[0].get('Lotto', '')).strip())
            try:
                di = candidati[0].get('Data_Inizio')
                ds = candidati[0].get('Scadenza')
                if di:
                    dt = _parse_date_any(di)
                    if dt:
                        data_inizio = dt.strftime('%d/%m/%Y')
                if ds:
                    dts = _parse_date_any(ds)
                    if dts:
                        data_scadenza = dts.strftime('%d/%m/%Y')
            except Exception:
                pass
    if not lotto:
        if cat == 'interno':
            lotto = f"L{datetime.now().strftime('%d%d%m%y')}{sigla}"
        else:
            return jsonify({"status": "error", "message": "Lotto esterno mancante."}), 400
    if data_inizio is None:
        data_inizio = datetime.now().strftime('%d/%m/%Y')
    if data_scadenza is None:
        data_scadenza = _calcola_scadenza_da_anagrafica(p_info) or "N/D"
    labels = [{'nome': nome, 'lotto': lotto, 'data_inizio': data_inizio, 'data_scadenza': data_scadenza, 'qta': qta}]
    pdf = genera_pdf_multi(labels)
    return Response(pdf, mimetype="application/pdf", headers={"Content-Disposition": f"inline; filename=ristampa_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf", "Content-Length": str(len(pdf))})

@app.route('/cancella_prodotto_per_tipo', methods=['POST'])
def cancella_prodotto_per_tipo():
    dati = request.json or {}
    nome = (dati.get('nome') or '').strip()
    qta = int(dati.get('quantita') or 0)
    if not nome or qta < 1:
        return jsonify({"status": "error", "message": "Dati non validi."}), 400
    _, giac, sh = _leggi_db_cached(force_refresh=True)
    if not sh:
        return jsonify({"status": "error", "message": "Errore connessione database."}), 500
    ws = sh.worksheet("GIACENZE")
    righe = [g for g in giac if g['Prodotto'].strip().lower() == nome.lower()]
    def parse_date(s):
        try:
            return datetime.strptime(str(s), '%Y-%m-%d')
        except Exception:
            return datetime.now()
    righe.sort(key=lambda x: parse_date(x.get('Data_Inizio') or ''))
    da_rimuovere = qta
    try:
        for r in righe:
            if da_rimuovere <= 0:
                break
            rid = r['riga_id']
            q = int(r.get('Quantità_Attuale', 0))
            if q <= 0:
                continue
            if q > da_rimuovere:
                ws.update_cell(rid, 2, q - da_rimuovere)
                da_rimuovere = 0
                break
            else:
                ws.update_cell(rid, 2, 0)
                da_rimuovere -= q
        invalidate_cache()
        return jsonify({"status": "success", "cancellate": qta - da_rimuovere})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/cancella_tutto_magazzino', methods=['POST'])
def cancella_tutto_magazzino():
    _, giac, sh = _leggi_db_cached(force_refresh=True)
    if not sh:
        return jsonify({"status": "error", "message": "Errore connessione database."}), 500
    ws = sh.worksheet("GIACENZE")
    try:
        values = ws.get_all_values()
        n = len(values)
        for i in range(n, 1, -1):
            ws.delete_rows(i)
        invalidate_cache()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/magazzino/prodotto/<nome>')
def dettaglio_prodotto(nome):
    _, giac, _ = _leggi_db_cached(force_refresh=True)
    righe = [g for g in giac if g['Prodotto'].strip().lower() == nome.strip().lower()]
    righe.sort(key=lambda x: (str(x.get('Lotto', '')), str(x.get('Data_Inizio', ''))))
    return render_template('dettaglio_prodotto.html', nome_prodotto=nome, righe=righe)

@app.route('/rimuovi_quantita', methods=['POST'])
def rimuovi_quantita():
    dati = request.json or {}
    riga_id = int(dati.get('riga_id') or 0)
    quantita = int(dati.get('quantita') or 0)
    if riga_id < 2 or quantita < 1:
        return jsonify({"status": "error", "message": "Dati non validi."}), 400
    _, _, sh = _leggi_db_cached(force_refresh=True)
    if not sh:
        return jsonify({"status": "error", "message": "Errore connessione database."}), 500
    ws = sh.worksheet("GIACENZE")
    try:
        current = int(ws.cell(riga_id, 2).value or 0)
        new_val = max(0, current - quantita)
        ws.update_cell(riga_id, 2, new_val)
        invalidate_cache()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/elimina_prodotto/<int:riga_id>', methods=['POST'])
def elimina_prodotto(riga_id):
    _, _, sh = _leggi_db_cached(force_refresh=True)
    if not sh:
        return jsonify({"status": "error", "message": "Errore connessione database."}), 500
    ws = sh.worksheet("GIACENZE")
    try:
        if riga_id >= 2:
            ws.delete_rows(riga_id)
            invalidate_cache()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/cache/refresh', methods=['POST'])
def refresh_cache():
    """Forza il refresh della cache (usare dopo modifiche esterne)."""
    try:
        invalidate_cache()
        anag, giac, sh = _leggi_db_cached(force_refresh=True)
        return jsonify({
            "status": "success", 
            "prodotti": len(anag), 
            "giacenze": len(giac)
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route('/api/cache/status', methods=['GET'])
def cache_status():
    """Restituisce lo stato della cache."""
    with _cache_lock:
        is_valid = _is_cache_valid()
        elapsed = 0
        if _cache['timestamp']:
            elapsed = (datetime.now() - _cache['timestamp']).total_seconds()
        return jsonify({
            "valid": is_valid,
            "prodotti_cached": len(_cache['anagrafica']),
            "giacenze_cached": len(_cache['giacenze']),
            "seconds_since_update": round(elapsed, 1),
            "ttl_seconds": CACHE_TTL_SECONDS
        })


# Precarica dati all'avvio dell'app
print("Precaricamento dati...")
try:
    _leggi_db_cached(force_refresh=True)
    print("Cache pronta!")
except Exception as e:
    print(f"Precaricamento non riuscito: {e}")


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug_env = str(os.environ.get('FLASK_DEBUG', '')).strip().lower()
    debug = debug_env in ('1', 'true', 'yes', 'on')
    app.run(host='0.0.0.0', port=port, debug=debug)
