# Avisos de terceros

BlenderFarm Lite © 2026 Zanix. Licencia: ver [LICENSE](LICENSE) (MIT + Commons Clause).

## Blender
La imagen Docker publicada (`ghcr.io/<usuario>/blenderfarm`) y el script `install.sh` incluyen o descargan
**Blender**, software libre bajo **GPL-3.0-or-later** © Blender Foundation.

- Sitio oficial: https://www.blender.org
- Código fuente de cada versión: https://download.blender.org/source/ y https://projects.blender.org/blender/blender
- Este proyecto **no modifica** Blender: lo descarga sin cambios de download.blender.org y lo ejecuta como un programa aparte.
- Blender y el logotipo de Blender son marcas de la Blender Foundation. Este proyecto no está afiliado ni respaldado por ella.

## server/render_job.py
Se ejecuta dentro de Blender y usa su API de Python (`bpy`), por lo que se distribuye bajo **GPL-3.0-or-later**
(texto completo en [LICENSES/GPL-3.0-or-later.txt](LICENSES/GPL-3.0-or-later.txt)). La cláusula de no venta de `LICENSE` no se aplica a ese archivo.

## Otros componentes de la imagen
La imagen se basa en Ubuntu 24.04 e instala paquetes del sistema (Python 3, OpenSSH, bibliotecas gráficas, etc.), cada uno con su propia licencia.
