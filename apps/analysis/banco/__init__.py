"""
Banco de similitud: casos con respuesta conocida (qué se copió y de dónde)
para calificar el motor sin depender de un solo documento real.

- `textos`: oraciones, pasajes y paráfrasis por reglas (sin IA).
- `generador`: arma los casos con semilla fija y su JSON de respuestas.
- `calificador`: compara lo marcado por el motor con las respuestas.

Los tres módulos son puros (sin BD ni red); el comando
`benchmark_similarity --banco ...` los conecta con la BD y los archivos.
"""
