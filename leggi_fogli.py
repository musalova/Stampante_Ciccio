import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import os
import json

_client = None
_scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

def get_client():
    global _client
    if _client is not None:
        return _client
    cred_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if cred_json:
        creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(cred_json), _scope)
    else:
        cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or os.getenv("CRED_PATH") or "credenziali.json"
        if not os.path.exists(cred_path):
            raise RuntimeError("Credenziali mancanti")
        creds = ServiceAccountCredentials.from_json_keyfile_name(cred_path, _scope)
    _client = gspread.authorize(creds)
    return _client

def genera_lotto_interno(sigla):
    # Regola: GiornoGiornoMeseAnno + Sigla (es. 18180226SC)
    oggi = datetime.now()
    data_str = oggi.strftime("%d%d%m%y")
    return f"{data_str}{sigla}"

def trova_lotto_fifo(giacenze, nome_prodotto):
    # Cerca nelle giacenze il primo lotto disponibile per quel prodotto
    for riga in giacenze:
        if str(riga.get('Prodotto', '')).strip().lower() == nome_prodotto.lower():
            # Cerca nella colonna 'Lotto_Originale' (quello del fornitore)
            lotto = riga.get('Lotto_Originale') or riga.get('Lotto')
            if lotto:
                return lotto
    return None

if __name__ == "__main__":
    try:
        cartella = client.open("Database_Ciccio_Lumia")
        anagrafica = cartella.worksheet("ANAGRAFICA").get_all_records()
        giacenze = cartella.worksheet("GIACENZE").get_all_records()
        print(f"\n--- STAMPA LINEA DEL GIORNO: {datetime.now().strftime('%d/%m/%Y')} ---")
        print("-" * 60)
        for p in anagrafica:
            if str(p.get('OBBLIGATORIO_GIORNALIERO', '')).lower() == 'si':
                nome = p.get('PRODOTTO', 'N/D')
                categoria = str(p.get('CATEGORIA', '')).lower()
                sigla = p.get('SIGLA', '')
                if 'interno' in categoria:
                    lotto = genera_lotto_interno(sigla)
                else:
                    lotto = trova_lotto_fifo(giacenze, nome) or "NON TROVATO!"
                try:
                    quantita = int(p.get('Pezzi_in_Linea', 1)) if p.get('Pezzi_in_Linea') else 1
                except:
                    quantita = 1
                print(f"STAMPA: {nome.ljust(18)} | LOTTO: {lotto.ljust(15)} | ETICHETTE: {quantita}")
        print("-" * 60)
        print("Fine elaborazione.")
    except Exception as e:
        print(f"C'è un errore: {e}")
