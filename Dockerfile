ARG BUILD_FROM
FROM $BUILD_FROM

WORKDIR /

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

COPY run.sh /run.sh
RUN chmod +x /run.sh

COPY blink_mqtt/ /blink_mqtt/
COPY templates/ /templates/

CMD ["/run.sh"]
