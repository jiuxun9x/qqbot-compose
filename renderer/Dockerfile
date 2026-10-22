# Official Debian base; no third-party render image or browser runtime.
FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get -o Acquire::Retries=1 -o Acquire::http::Timeout=30 update \
    && apt-get install -y --no-install-recommends \
       python3 python3-pil pandoc poppler-utils ca-certificates unzip \
       texlive-xetex texlive-lang-chinese texlive-science \
       texlive-bibtex-extra texlive-fonts-recommended \
    && rm -rf /var/lib/apt/lists/*
RUN apt-get -o Acquire::Retries=1 -o Acquire::http::Timeout=30 update \
    && apt-get install -y --no-install-recommends fonts-texgyre texlive-plain-generic \
    && rm -rf /var/lib/apt/lists/*
COPY assets/*.zip /opt/font-archives/
COPY install-fonts.py /opt/install-fonts.py
RUN python3 /opt/install-fonts.py \
    && rm -r /opt/font-archives /opt/install-fonts.py
COPY assets/elegantbook.cls assets/preamble.tex /opt/renderer/assets/
COPY md-preamble.tex blocks.lua server.py /opt/renderer/
ENV HOME=/tmp TEXMFVAR=/tmp/texmf-var TEXMFCONFIG=/tmp/texmf-config \
    TEXMFOUTPUT=/tmp openin_any=p openout_any=p shell_escape=f \
    PYTHONUNBUFFERED=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
USER 65532:65532
WORKDIR /opt/renderer
EXPOSE 8080
CMD ["python3", "/opt/renderer/server.py"]
