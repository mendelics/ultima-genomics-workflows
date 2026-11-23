version 1.0

struct GlobalVariables {
  String monitoring_script
  String str_genotyper_docker
}
workflow Globals {
  input {
  GlobalVariables glob ={
        "monitoring_script": "gs://ULTIMA_REFERENCES/monitor_1.0.sh",
        "str_genotyper_docker": "ultimagenomics/str_genotyper:1.2.0"
}
}

  output {
    GlobalVariables global_dockers = glob
  }
}