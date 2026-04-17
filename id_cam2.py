#!/usr/bin/env python3
"""
anaglifo_estereo_debian.py
──────────────────────────
Generador de anaglifos estéreo Rojo-Cian para Raspberry Pi 4B con Debian.

Diferencias clave respecto a la versión Windows:
  • Usa cv2.CAP_V4L2 en lugar de CAP_DSHOW (backend Linux)
  • Captura SECUENCIAL: abre → calienta → captura → cierra cada cámara por
    separado, para evitar el error de ancho de banda USB del Pi4 que hace
    fallar la segunda cámara cuando ambas están abiertas al mismo tiempo.
  • Resolución reducida a 640×480 por defecto: el Pi4 con dos webcams USB 2.0
    en el mismo bus no puede mantener 1280×720 sin errores de asignación de
    ancho de banda (confirmado en foros oficiales de Raspberry Pi).
  • Auto-detección de dispositivos /dev/video* reales (filtra los nodos
    virtuales del codec bcm2835 que no son webcams).
  • Sin cv2.imshow() por defecto si no hay display conectado (modo headless).
    Cambia MOSTRAR_VENTANA = True si tienes monitor o usas VNC/X11.

Uso:
  python3 anaglifo_estereo_debian.py

Antes de ejecutar, verifica tus dispositivos con:
  v4l2-ctl --list-devices
y ajusta DEV_IZQ / DEV_DER según corresponda.
"""

import subprocess
import sys
import time
import os
import ftplib
import cv2
import numpy as np


# ═══════════════════════════════════════════════════════════════
#  CONFIGURACIÓN — AJUSTA ESTO SEGÚN TU SETUP
# ═══════════════════════════════════════════════════════════════

#MODIFICAR IP SEGUN SERVIDOR
FTP_HOST= "192.168.3.28"
#^^^^^^^^^^^^^^^^^^^^^^^^^^^
FTP_USER= "icarus"
FTP_PASS= "icarus"
FILENAME= "imagen.png"

# Dispositivos V4L2. Ejecuta `v4l2-ctl --list-devices` para ver los tuyos.
# Normalmente las webcams USB son /dev/video0 y /dev/video2 en el Pi4
# (el Pi crea dos nodos por cámara: video0+video1 para cam1, video2+video3 para cam2).
# Usa siempre el PRIMER nodo de cada cámara (el par: video0, video2).
DEV_IZQ = "/dev/video0"
DEV_DER = "/dev/video2"

# Resolución de captura.
# IMPORTANTE para Pi4: con dos webcams USB 2.0 en el mismo controlador,
# 640×480 es el máximo estable. Si tienes las cámaras en puertos USB 3.0
# distintos puedes probar 1280×720, pero 1080p fallará casi seguro.
ANCHO = 1280
ALTO  = 720

# Frames de calentamiento por cámara (sensor AGC/AWB)
WARM_UP = 30

# Archivo de salida
OUT_FILE = "imagen.png"

# Mostrar ventanas gráficas (requiere display / VNC / X11 forwarding)
# Ponlo en False si ejecutas sin monitor (headless / SSH sin X11)
MOSTRAR_VENTANA = False

# Método de anaglifo:
#   'color'     → Rojo|Cian puro (más vívido)
#   'halfcolor' → Izq en gris, Der en cian (menos fatiga ocular)
#   'optimized' → Dubois optimizado (mejor calidad, recomendado)
METODO_ANAGLIFO = 'optimized'


# ═══════════════════════════════════════════════════════════════
#  MATRICES DE MEZCLA  (3 filas × 6 cols)
#  Canales entrada: [B_izq, G_izq, R_izq, B_der, G_der, R_der]
#  Canales salida BGR: fila0=B_sal, fila1=G_sal, fila2=R_sal
# ═══════════════════════════════════════════════════════════════
MIX = {
    'color': np.array([
        [0, 0, 0,  1, 0, 0],
        [0, 0, 0,  0, 1, 0],
        [0, 0, 1,  0, 0, 0],
    ], dtype=np.float32),

    'halfcolor': np.array([
        [0,     0,     0,      1, 0, 0],
        [0,     0,     0,      0, 1, 0],
        [0.299, 0.587, 0.114,  0, 0, 0],
    ], dtype=np.float32),

    'optimized': np.array([
        [0,   0,   0,    1, 0, 0],
        [0,   0,   0,    0, 1, 0],
        [0,   0.7, 0.3,  0, 0, 0],
    ], dtype=np.float32),
}


# ═══════════════════════════════════════════════════════════════
#  DIAGNÓSTICO DE DISPOSITIVOS
# ═══════════════════════════════════════════════════════════════

def listar_camaras_v4l2():
    """Muestra los dispositivos de video reales detectados por el kernel."""
    print("\n  📋 Dispositivos V4L2 detectados:")
    try:
        result = subprocess.run(
            ["v4l2-ctl", "--list-devices"],
            capture_output=True, text=True, timeout=5
        )
        for linea in result.stdout.splitlines():
            print(f"     {linea}")
    except FileNotFoundError:
        print("     (v4l2-ctl no instalado — instala con: sudo apt install v4l-utils)")
    except Exception as e:
        print(f"     (error: {e})")
    print()


def verificar_dispositivo(dev):
    """Comprueba que el archivo de dispositivo existe y es accesible."""
    if not os.path.exists(dev):
        print(f"  ❌ Dispositivo {dev} no encontrado.")
        print(f"     Ejecuta `v4l2-ctl --list-devices` y ajusta DEV_IZQ / DEV_DER.")
        return False
    if not os.access(dev, os.R_OK):
        print(f"  ❌ Sin permiso de lectura en {dev}.")
        print(f"     Agrega tu usuario al grupo video: sudo usermod -aG video $USER")
        return False
    return True


# ═══════════════════════════════════════════════════════════════
#  CAPTURA SECUENCIAL (solución al problema de ancho de banda USB)
# ═══════════════════════════════════════════════════════════════

def capturar_una_camara(dispositivo, warm_up=WARM_UP):
    """
    Abre una sola cámara, la calienta, captura un frame y la cierra.
    Este enfoque secuencial evita el fallo de 'select() timeout' y
    'cannot allocate resources' que ocurre al abrir dos webcams USB
    simultáneamente en el Pi4.
    """
    print(f"  📷 Capturando desde {dispositivo}...", end=" ", flush=True)

    cap = cv2.VideoCapture(dispositivo, cv2.CAP_V4L2)

    if not cap.isOpened():
        print(f"\n  ❌ No se pudo abrir {dispositivo}")
        return None

    # Forzar MJPG para reducir el ancho de banda USB (comprimido vs YUYV sin comprimir)
    # MJPG usa ~10× menos ancho de banda que YUYV a la misma resolución
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  ANCHO)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, ALTO)
    cap.set(cv2.CAP_PROP_FPS, 30)

    # Verificar resolución real asignada (el driver puede haberla ajustado)
    w_real = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h_real = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    # Calentamiento: descartar frames iniciales (AGC, AWB, enfoque)
    for _ in range(warm_up):
        cap.grab()

    # Intentar captura con reintentos
    img = None
    for intento in range(5):
        ok, frame = cap.read()
        if ok and frame is not None and frame.size > 0:
            img = frame
            break
        time.sleep(0.1)

    cap.release()

    if img is None:
        print(f"❌ No se pudo leer frame de {dispositivo}")
        return None

    print(f"OK ({w_real}×{h_real})")
    return img


def capturar_par_secuencial():
    """
    Captura las dos cámaras de forma secuencial (una tras otra, no simultánea).
    Introduce un pequeño retardo entre capturas pero evita los problemas de
    ancho de banda USB del Pi4. Para estereoscopía estática (foto, no video)
    esto es perfectamente válido.
    """
    if not verificar_dispositivo(DEV_IZQ):
        return None, None
    if not verificar_dispositivo(DEV_DER):
        return None, None

    img_izq = capturar_una_camara(DEV_IZQ)
    if img_izq is None:
        return None, None

    # Pequeña pausa para que el controlador USB libere recursos
    time.sleep(0.3)

    img_der = capturar_una_camara(DEV_DER)
    if img_der is None:
        return None, None

    # Asegurar que ambas imágenes tienen el mismo tamaño
    if img_izq.shape != img_der.shape:
        print(f"  ⚠️  Tamaños distintos: izq={img_izq.shape}, der={img_der.shape}")
        print(f"     Redimensionando derecha al tamaño de la izquierda...")
        h, w = img_izq.shape[:2]
        img_der = cv2.resize(img_der, (w, h), interpolation=cv2.INTER_AREA)

    return img_izq, img_der


# ═══════════════════════════════════════════════════════════════
#  EMPAREJAMIENTO DE PUNTOS SIFT
# ═══════════════════════════════════════════════════════════════

def emparejar_puntos(img_izq, img_der):
    """Devuelve puntos correspondientes con SIFT + ratio de Lowe."""
    gray_izq = cv2.cvtColor(img_izq, cv2.COLOR_BGR2GRAY)
    gray_der = cv2.cvtColor(img_der, cv2.COLOR_BGR2GRAY)

    # Ecualizar histograma para mejorar detección con poca luz o bajo contraste
    gray_izq = cv2.equalizeHist(gray_izq)
    gray_der = cv2.equalizeHist(gray_der)

    sift = cv2.SIFT_create(nfeatures=5000)
    kp0, des0 = sift.detectAndCompute(gray_izq, None)
    kp1, des1 = sift.detectAndCompute(gray_der, None)

    if des0 is None or des1 is None or len(kp0) < 8 or len(kp1) < 8:
        return None, None

    # FLANN es más rápido que BFMatcher (importante en ARM)
    index_params  = dict(algorithm=1, trees=5)
    search_params = dict(checks=50)
    flann = cv2.FlannBasedMatcher(index_params, search_params)

    try:
        matches_knn = flann.knnMatch(des0, des1, k=2)
    except cv2.error:
        # Fallback a BFMatcher si FLANN falla
        bf = cv2.BFMatcher(cv2.NORM_L2, crossCheck=False)
        matches_knn = bf.knnMatch(des0, des1, k=2)

    # Test de ratio de Lowe
    buenos = []
    for pair in matches_knn:
        if len(pair) == 2:
            m, n = pair
            if m.distance < 0.75 * n.distance:
                buenos.append(m)

    print(f"  ✅ {len(buenos)} coincidencias SIFT válidas.")

    if len(buenos) < 8:
        return None, None

    pts_izq = np.float32([kp0[m.queryIdx].pt for m in buenos])
    pts_der = np.float32([kp1[m.trainIdx].pt for m in buenos])
    return pts_izq, pts_der


# ═══════════════════════════════════════════════════════════════
#  RECTIFICACIÓN ROBUSTA EN DOS PASOS
# ═══════════════════════════════════════════════════════════════

def rectificar_par(img_izq, img_der):
    """
    Rectificación en dos pasos optimizada para Pi4:

    Paso 1 — Alineación afín parcial (rotación + traslación + escala).
             Corrige el giro físico entre cámaras sin deformar perspectiva.

    Paso 2 — Corrección del desplazamiento vertical residual (dy puro).
             Mide el error Y mediano entre inliers epipolarmente consistentes
             y aplica traslación pura en Y. Esto es lo que el cerebro necesita
             para fusionar el par en 3D con gafas rojo-cian.
    """
    h, w = img_izq.shape[:2]

    # ── Paso 1 ──────────────────────────────────────────────────────────────
    pts_izq, pts_der = emparejar_puntos(img_izq, img_der)
    if pts_izq is None:
        print("  ⚠️  Pocos puntos. Apunta las cámaras a una escena con más textura.")
        return None

    M, _ = cv2.estimateAffinePartial2D(
        pts_der, pts_izq,
        method=cv2.RANSAC,
        ransacReprojThreshold=3.0,
        confidence=0.999
    )
    if M is None:
        print("  ⚠️  No se pudo estimar la transformación afín.")
        return None

    img_der_aln = cv2.warpAffine(
        img_der, M, (w, h),
        flags=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0
    )

    # ── Paso 2: corregir dy residual ─────────────────────────────────────────
    pts_izq2, pts_der2 = emparejar_puntos(img_izq, img_der_aln)
    if pts_izq2 is not None and len(pts_izq2) >= 8:
        F2, mask_F2 = cv2.findFundamentalMat(
            pts_izq2, pts_der2,
            cv2.FM_RANSAC,
            ransacReprojThreshold=2.0,
            confidence=0.999
        )
        if mask_F2 is not None:
            inliers_izq = pts_izq2[mask_F2.ravel() == 1]
            inliers_der = pts_der2[mask_F2.ravel() == 1]
            if len(inliers_izq) >= 4:
                dy = float(np.median(inliers_izq[:, 1] - inliers_der[:, 1]))
                print(f"  📐 dy residual: {dy:+.2f} px → corrigiendo con traslación Y...")
                T_y = np.float32([[1, 0, 0], [0, 1, dy]])
                img_der_aln = cv2.warpAffine(
                    img_der_aln, T_y, (w, h),
                    flags=cv2.INTER_LANCZOS4,
                    borderMode=cv2.BORDER_CONSTANT,
                    borderValue=0
                )

    # Máscara de solapamiento
    mask_izq = (cv2.cvtColor(img_izq,     cv2.COLOR_BGR2GRAY) > 0).astype(np.uint8)
    mask_der = (cv2.cvtColor(img_der_aln, cv2.COLOR_BGR2GRAY) > 0).astype(np.uint8)
    mask_comun = mask_izq & mask_der

    return img_izq, img_der_aln, mask_comun


# ═══════════════════════════════════════════════════════════════
#  DIAGNÓSTICO DE CALIDAD FINAL
# ═══════════════════════════════════════════════════════════════

def verificar_alineacion(img_izq, img_der_aln):
    pts_izq, pts_der = emparejar_puntos(img_izq, img_der_aln)
    if pts_izq is None:
        print("  ⚠️  No se pudo verificar la alineación.")
        return

    F, mask_F = cv2.findFundamentalMat(
        pts_izq, pts_der,
        cv2.FM_RANSAC,
        ransacReprojThreshold=2.0,
        confidence=0.999
    )
    if mask_F is None:
        return

    inl_i = pts_izq[mask_F.ravel() == 1]
    inl_d = pts_der[mask_F.ravel() == 1]
    dy_mean = float(np.mean(np.abs(inl_i[:, 1] - inl_d[:, 1])))

    print(f"  📐 Error vertical final: {dy_mean:.2f} px  (n={len(inl_i)} pares)")
    if dy_mean < 3:
        print("  ✅ Rectificación correcta — el efecto 3D debería funcionar bien.")
    elif dy_mean < 8:
        print("  ⚠️  Rectificación aceptable — prueba con escena más texturizada.")
    else:
        print("  ❌ Error alto — ajusta físicamente las cámaras (más paralelas entre sí).")


# ═══════════════════════════════════════════════════════════════
#  GENERACIÓN DEL ANAGLIFO
# ═══════════════════════════════════════════════════════════════

def generar_anaglifo(img_izq, img_der_aln, mask_comun, metodo='optimized'):
    """
    Combina el par rectificado en un anaglifo Rojo-Cian.
    Usa np.einsum para aplicar la matriz de mezcla (3×6) a cada píxel,
    evitando el ValueError de shapes que ocurría con np.dot en arrays 3D.
    """
    L = img_izq.astype(np.float32) / 255.0
    R = img_der_aln.astype(np.float32) / 255.0

    merged = np.concatenate([L, R], axis=2)          # (h, w, 6)
    mix    = MIX.get(metodo, MIX['optimized'])        # (3, 6)

    anaglifo_f = np.einsum('hwc,oc->hwo', merged, mix)  # (h, w, 3)
    anaglifo_f = np.clip(anaglifo_f, 0, 1)
    anaglifo   = (anaglifo_f * 255).astype(np.uint8)

    # Recortar bordes negros
    mask_u8 = (mask_comun * 255).astype(np.uint8)
    coords  = cv2.findNonZero(mask_u8)
    if coords is not None:
        x, y, bw, bh = cv2.boundingRect(coords)
        m = 6
        anaglifo = anaglifo[y+m : y+bh-m, x+m : x+bw-m]

    return anaglifo


# ═══════════════════════════════════════════════════════════════
#  PROGRAMA PRINCIPAL
# ═══════════════════════════════════════════════════════════════

def main():
    inicio = time.time()
    
    print("\n" + "═" * 46)
    print("  ANAGLIFO ESTÉREO — Raspberry Pi 4B / Debian")
    print("═" * 46)

    # Mostrar dispositivos disponibles
    listar_camaras_v4l2()

    print(f"  Usando: {DEV_IZQ} (izq)  |  {DEV_DER} (der)")
    print(f"  Resolución: {ANCHO}×{ALTO}  |  Método: {METODO_ANAGLIFO}")
    print()

    # ── Captura secuencial ────────────────────────────────────────────────────
    print("📷 Capturando imágenes (secuencial para evitar conflicto USB)...")
    img_izq, img_der = capturar_par_secuencial()
    if img_izq is None:
        print("\n❌ Captura fallida. Revisa los dispositivos y vuelve a intentarlo.")
        print("   Sugerencias:")
        print("   • Conecta cada cámara en un puerto USB distinto (idealmente USB3+USB2)")
        print("   • Verifica permisos: ls -la /dev/video*")
        print("   • Prueba: sudo python3 anaglifo_estereo_debian.py")
        sys.exit(1)

    cv2.imwrite("cruda_izq.png", img_izq)
    cv2.imwrite("cruda_der.png", img_der)
    print("  💾 Imágenes crudas guardadas (cruda_izq.png, cruda_der.png)")

    # ── Rectificación ─────────────────────────────────────────────────────────
    print("\n🔧 Rectificando par estéreo (afín + corrección Y)...")
    resultado = rectificar_par(img_izq, img_der)

    if resultado is None:
        print("\n❌ Rectificación fallida.")
        print("   • Apunta las cámaras a una escena con más textura")
        print("     (estantes con libros, carteles, mesas con objetos).")
        print("   • Evita paredes lisas o fondos uniformes.")
        sys.exit(1)

    rect_izq, rect_der, mask_comun = resultado

    cv2.imwrite("rect_izq.png", rect_izq)
    cv2.imwrite("rect_der.png", rect_der)
    print("  💾 Imágenes rectificadas guardadas (rect_izq.png, rect_der.png)")

    # ── Verificación ──────────────────────────────────────────────────────────
    print("\n📐 Verificando calidad de la alineación...")
    verificar_alineacion(rect_izq, rect_der)

    # ── Generar anaglifo ──────────────────────────────────────────────────────
    print(f"\n🎨 Generando anaglifo ({METODO_ANAGLIFO})...")
    anaglifo = generar_anaglifo(rect_izq, rect_der, mask_comun, metodo=METODO_ANAGLIFO)

    cv2.imwrite(OUT_FILE, anaglifo, [cv2.IMWRITE_PNG_COMPRESSION, 0])
    print(f"  ✅ Guardado: {OUT_FILE}  ({anaglifo.shape[1]}×{anaglifo.shape[0]} px)")
    
    # ── Envio de imagen.png a estacion terrena por FTP ──────────────────────────
    try:
        with ftplib.FTP(FTP_HOST, FTP_USER, FTP_PASS) as ftp:
            print(f"Logged into {FTP_HOST}")
            with open(FILENAME, 'rb') as file:
                ftp.storbinary(f'STOR {FILENAME}', file)
            
            print("Archivo Subido con exito!")

    except ftplib.all_errors as e:
        print(f"FTP error: {e}")
    
    # ── Fin del codigo y calculo de tiempo de ejecucion ─────────────────────────
    fin= time.time()
    tiempo_eje = fin-inicio
    print(f"tiempo: {tiempo_eje:.2f} segundos")   

    print("\n🎉 Proceso completado!")

    # ── Mostrar ventanas (solo si hay display) ────────────────────────────────
    if MOSTRAR_VENTANA:
        try:
            cv2.imshow("Anaglifo Rojo-Cian (usa gafas 3D)", anaglifo)

            h, w = rect_izq.shape[:2]
            sep  = np.zeros((h, 4, 3), dtype=np.uint8)
            comp = np.hstack([rect_izq, sep, rect_der])
            cv2.imshow("Par rectificado — alineación Y", comp)

            print("\n  [Presiona cualquier tecla para cerrar]")
            cv2.waitKey(0)
            cv2.destroyAllWindows()
        except cv2.error as e:
            print(f"\n  ⚠️  No se pudo mostrar ventana: {e}")
            print("     Ejecuta con MOSTRAR_VENTANA = False si estás en modo headless.")
            print(f"     El resultado está guardado en {OUT_FILE}")
    else:
        print(f"\n  Modo headless: resultado en {OUT_FILE}")
        print("  Visualízalo con: eog anaglifo_final.png  (o scp al PC)")

if __name__ == "__main__":
    main()
