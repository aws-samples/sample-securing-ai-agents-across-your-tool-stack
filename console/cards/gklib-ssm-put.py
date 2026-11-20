# SOURCE: scripts/gklib.py L231-232
def ssm_put(name, value):
    SESSION.client("ssm").put_parameter(
        Name=name, Value=value, Type="String", Overwrite=True)
