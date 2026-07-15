# HatchPlot

HatchPlot is a web application and Python backend to process grayscale and multi-color SVGs into crosshatched G-code paths for physical plotters.

## Features


- Converts SVG paths to optimized G-code


- Supports grayscale and multi-color processing


- Generates precise crosshatched toolpaths


- Hardware acceleration support (CUDA)


- Containerized Nginx/Alpine frontend and Python backend



## Repository Structure


- `/frontend`: Contains the web interface, Nginx configuration, and application logic (`app.js`, `converter.js`).


- `/backend`: Contains the Python processing API, including `converter_engines.py` and `linedraw_engine.py`.


- `/licenses`: Contains relevant GPL notices and third-party license information.



## Installation

HatchPlot is deployed using Docker Compose.


1. Clone the repository:

```
git clone https://github.com/jcarletto27/hatchplot.git cd hatchplot   

```


1. Configure your environment:

```
cp .env.template .env   

```


1. Start the application:

```
docker compose up -d   

```



## Hardware Acceleration & Profiles

Alternative Docker Compose files are provided for different hardware configurations and licensing constraints. Run them using the `-f` flag:


- **Standard (CPU)**: `compose.yml`


- **GPU Accelerated**: `compose.gpu.yml`


- **GPL Dependencies (CPU)**: `compose.gpl.yml`


- **GPL Dependencies (GPU)**: `compose.gpu-gpl.yml`



Example for starting the GPU-accelerated container:

```
docker compose -f compose.gpu.yml up -d   

```

## Licenses

Refer to `THIRD_PARTY_NOTICES.md` and `OPTIONAL_GPL_NOTICES.md` for library and license details.
