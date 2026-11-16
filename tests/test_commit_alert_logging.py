"""Real installed SDK import orders must not silence commitment-space alerts."""
import subprocess
import sys
import textwrap
import pytest

@pytest.mark.parametrize('sdk_first',[False,True])
def test_commit_alerts_survive_sdk_initialization(sdk_first):
    imports = ('import bittensor\nimport bundle_commit_guard as g' if sdk_first
               else 'import bundle_commit_guard as g\nimport bittensor')
    script = imports + '\n' + textwrap.dedent('''
        import io,logging
        root=logging.getLogger()
        before=(root.level,tuple(root.handlers),logging.getLogger('bittensor').level)
        sink=io.StringIO();handler=logging.StreamHandler(sink)
        g.logger.addHandler(handler)
        try:
            g.alert(g.ALERT_FROZEN,'synthetic frozen space')
            g.alert(g.ALERT_SPACE,'synthetic live space')
            text=sink.getvalue()
            assert text.count(g.ALERT_FROZEN)==1,repr(text)
            assert text.count(g.ALERT_SPACE)==1,repr(text)
            assert (root.level,tuple(root.handlers),logging.getLogger('bittensor').level)==before
        finally:
            g.logger.removeHandler(handler)
    ''')
    out=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,timeout=30)
    assert out.returncode==0,out.stdout+out.stderr
