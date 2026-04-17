## Ejecutar el servidor FTP para que se envie la imagen a la carpeta publica. imagen de docker usada vsftpd
comando usado:

```
docker run -d -v ~/023/icarus/etIcarus-react/public/icarus:/home/vsftpd \
-p 20:20 -p 21:21 -p 21100-21110:21100-21110 -e FTP_USER=icarus -e FTP_PASS=icarus \
-e PASV_MIN_PORT=21100 -e PASV_MAX_PORT=21110 -e LOCAL_UMASK=022 \
--name vsftpd --restart=always fauria/vsftpd
```
atte: Equipo-ICARUS
