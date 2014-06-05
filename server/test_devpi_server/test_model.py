from __future__ import unicode_literals

import py
import pytest
from textwrap import dedent

from devpi_common.metadata import splitbasename
from devpi_common.archive import Archive, zip_dict
from devpi_server.model import InvalidIndexconfig, run_passwd
from py.io import BytesIO

pytestmark = [pytest.mark.writetransaction]


@pytest.fixture(params=[(), ("root/pypi",)])
def bases(request):
    return request.param

def register_and_store(stage, basename, content=b"123", name=None):
    assert py.builtin._isbytes(content), content
    n, version = splitbasename(basename)[:2]
    if name is None:
        name = n
    stage.register_metadata(dict(name=name, version=version))
    return stage.store_releasefile(name, version, basename, content)

def test_is_empty(model, keyfs):
    assert model.is_empty()
    user = model.create_user("user", "password", email="some@email.com")
    assert not model.is_empty()
    stage = model.getstage("user", "dev")
    assert stage is None
    user.create_stage("dev", bases=(), type="stage", volatile=False)
    assert not model.is_empty()
    stage = model.getstage("user/dev")
    stage.delete()
    user.delete()
    assert model.is_empty()

class TestStage:
    @pytest.fixture
    def stage(self, request, user):
        config = dict(index="world", bases=(),
                      type="stage", volatile=True)
        if "bases" in request.fixturenames:
            config["bases"] = request.getfuncargvalue("bases")
        return user.create_stage(**config)

    @pytest.fixture
    def user(self, model):
        return model.create_user("hello", password="123")

    def test_create_and_delete(self, model):
        user = model.create_user("hello", password="123")
        user.create_stage("world", bases=(), type="stage", volatile=False)
        user.create_stage("world2", bases=(), type="stage", volatile=False)
        stage = model.getstage("hello", "world2")
        stage.delete()
        assert model.getstage("hello", "world2") is None
        assert model.getstage("hello", "world") is not None

    def test_set_and_get_acl(self, model, stage):
        indexconfig = stage.ixconfig
        # check that "hello" was included in acl_upload by default
        assert indexconfig["acl_upload"] == ["hello"]
        stage = model.getstage("hello/world")
        # root cannot upload
        assert not stage.can_upload("root")

        # and we remove 'hello' from acl_upload ...
        stage.modify(acl_upload=[])
        # ... it cannot upload either
        stage = model.getstage("hello/world")
        assert not stage.can_upload("hello")

    def test_getstage_normalized(self, model):
        assert model.getstage("/root/pypi/").name == "root/pypi"

    def test_not_configured_index(self, model):
        stagename = "hello/world"
        assert model.getstage(stagename) is None

    def test_indexconfig_set_throws_on_unknown_base_index(self, stage, user):
        with pytest.raises(InvalidIndexconfig) as excinfo:
            user.create_stage(index="something",
                              bases=("root/notexists", "root/notexists2"),)
        messages = excinfo.value.messages
        assert len(messages) == 2
        assert "root/notexists" in messages[0]
        assert "root/notexists2" in messages[1]

    def test_indexconfig_set_throws_on_invalid_base_index(self, stage, user):
        with pytest.raises(InvalidIndexconfig) as excinfo:
            user.create_stage(index="world", bases=("root/dev/123",),)
        messages = excinfo.value.messages
        assert len(messages) == 1
        assert "root/dev/123" in messages[0]

    def test_indexconfig_set_normalizes_bases(self, user):
        stage = user.create_stage(index="world", bases=("/root/pypi/",))
        assert stage.ixconfig["bases"] == ("root/pypi",)

    def test_empty(self, stage, bases):
        assert not stage.getreleaselinks("someproject")
        assert not stage.getprojectnames()

    def test_10_metadata_name_mixup(self, stage, bases):
        stage._register_metadata({"name": "x-encoder", "version": "1.0"})
        key = stage.key_projconfig(name="x_encoder")
        with key.update() as projectconfig:
            versionconfig = projectconfig["1.0"] = {}
            versionconfig.update({"+files":
                {"x_encoder-1.0.zip": "%s/x_encoder/1.0/x_encoder-1.0.zip" %
                 stage.name}})
        with stage.key_projectnames.update() as projectnames:
            projectnames.add("x_encoder")

        names = stage.getprojectnames_perstage()
        assert len(names) == 2
        assert "x-encoder" in names
        assert "x_encoder" in names
        # also test import/export
        from devpi_server.importexport import Exporter
        tw = py.io.TerminalWriter()
        exporter = Exporter(tw, stage.xom)
        exporter.compute_global_projectname_normalization()

    def test_inheritance_simple(self, pypistage, stage):
        stage.modify(bases=("root/pypi",))
        pypistage.mock_simple("someproject", "<a href='someproject-1.0.zip' /a>")
        assert stage.getprojectnames() == ["someproject",]
        entries = stage.getreleaselinks("someproject")
        assert len(entries) == 1
        stage.register_metadata(dict(name="someproject", version="1.1"))
        assert stage.getprojectnames() == ["someproject",]

    def test_inheritance_twice(self, pypistage, stage, user):
        user.create_stage(index="dev2", bases=("root/pypi",))
        stage_dev2 = user.getstage("dev2")
        stage.modify(bases=(stage_dev2.name,))
        pypistage.mock_simple("someproject", 
                              "<a href='someproject-1.0.zip' /a>")
        register_and_store(stage_dev2, "someproject-1.1.tar.gz")
        register_and_store(stage_dev2, "someproject-1.2.tar.gz")
        entries = stage.getreleaselinks("someproject")
        assert len(entries) == 3
        assert entries[0].basename == "someproject-1.2.tar.gz"
        assert entries[1].basename == "someproject-1.1.tar.gz"
        assert entries[2].basename == "someproject-1.0.zip"
        assert stage.getprojectnames() == ["someproject",]

    def test_inheritance_normalize_multipackage(self, pypistage, stage):
        stage.modify(bases=("root/pypi",))
        pypistage.mock_simple("some-project", """
            <a href='some_project-1.0.zip' /a>
            <a href='some_project-1.0.tar.gz' /a>
        """)
        register_and_store(stage, "some_project-1.2.tar.gz",
                           name="some-project")
        register_and_store(stage, "some_project-1.2.tar.gz",
                           name="some-project")
        entries = stage.getreleaselinks("some-project")
        assert len(entries) == 3
        assert entries[0].basename == "some_project-1.2.tar.gz"
        assert entries[1].basename == "some_project-1.0.zip"
        assert entries[2].basename == "some_project-1.0.tar.gz"
        assert stage.getprojectnames() == ["some-project",]

    def test_getreleaselinks_inheritance_shadow(self, pypistage, stage):
        stage.modify(bases=("root/pypi",))
        pypistage.mock_simple("someproject",
            "<a href='someproject-1.0.zip' /a>")
        register_and_store(stage, "someproject-1.0.zip", b"123")
        stage.store_releasefile("someproject", "1.0",
                                "someproject-1.0.zip", b"123")
        entries = stage.getreleaselinks("someproject")
        assert len(entries) == 1
        assert entries[0].relpath.endswith("someproject-1.0.zip")

    def test_getreleaselinks_inheritance_shadow_egg(self, pypistage, stage):
        stage.modify(bases=("root/pypi",))
        pypistage.mock_simple("py",
        """<a href="http://bb.org/download/py.zip#egg=py-dev" />
           <a href="http://bb.org/download/master#egg=py-dev2" />
        """)
        register_and_store(stage, "py-1.0.tar.gz", b"123")
        entries = stage.getreleaselinks("py")
        assert len(entries) == 3
        e0, e1, e2 = entries
        assert e0.basename == "py-1.0.tar.gz"
        assert e1.basename == "py.zip"
        assert e2.basename == "master"

    def test_inheritance_error(self, pypistage, stage):
        stage.modify(bases=("root/pypi",))
        pypistage.mock_simple("someproject", status_code = -1)
        entries = stage.getreleaselinks("someproject")
        assert entries == -1
        #entries = stage.getprojectnames()
        #assert entries == -1

    def test_get_projectconfig_inherited(self, pypistage, stage):
        stage.modify(bases=("root/pypi",))
        pypistage.mock_simple("someproject",
            "<a href='someproject-1.0.zip' /a>")
        projectconfig = stage.get_projectconfig("someproject")
        assert "someproject-1.0.zip" in projectconfig["1.0"]["+files"]

    def test_store_and_get_releasefile(self, stage, bases):
        content = b"123"
        entry = register_and_store(stage, "some-1.0.zip", content)
        entries = stage.getreleaselinks("some")
        assert len(entries) == 1
        assert entries[0].md5 == entry.md5
        assert stage.getprojectnames() == ["some"]
        pconfig = stage.get_projectconfig("some")
        assert pconfig["1.0"]["+files"]["some-1.0.zip"].endswith("some-1.0.zip")

    def test_store_releasefile_fails_if_not_registered(self, stage):
        with pytest.raises(stage.MissesRegistration):
            stage.store_releasefile("someproject", "1.0",
                                    "someproject-1.0.zip", b"123")

    def test_project_config_shadowed(self, pypistage, stage):
        stage.modify(bases=("root/pypi",))
        pypistage.mock_simple("someproject",
            "<a href='someproject-1.0.zip' /a>")
        content = b"123"
        stage.store_releasefile("someproject", "1.0",
                                "someproject-1.0.zip", content)
        projectconfig = stage.get_projectconfig("someproject")
        files = projectconfig["1.0"]["+files"]
        link = list(files.values())[0]
        assert link.endswith("someproject-1.0.zip")
        assert projectconfig["1.0"]["+shadowing"]

    def test_store_and_delete_project(self, stage, bases):
        content = b"123"
        register_and_store(stage, "some-1.0.zip", content)
        pconfig = stage.get_projectconfig_perstage("some")
        assert pconfig["1.0"]
        stage.project_delete("some")
        pconfig = stage.get_projectconfig_perstage("some")
        assert not pconfig

    def test_store_and_delete_release(self, stage, bases):
        register_and_store(stage, "some-1.0.zip")
        register_and_store(stage, "some-1.1.zip")
        pconfig = stage.get_projectconfig_perstage("some")
        assert pconfig["1.0"] and pconfig["1.1"]
        stage.project_version_delete("some", "1.0")
        pconfig = stage.get_projectconfig_perstage("some")
        assert pconfig["1.1"] and "1.0" not in pconfig
        stage.project_version_delete("some", "1.1")
        assert not stage.project_exists("some")

    def test_releasefile_sorting(self, stage, bases):
        register_and_store(stage, "some-1.1.zip")
        register_and_store(stage, "some-1.0.zip")
        entries = stage.getreleaselinks("some")
        assert len(entries) == 2
        assert entries[0].basename == "some-1.1.zip"

    def test_getdoczip(self, stage, bases, tmpdir):
        assert not stage.get_doczip("pkg1", "version")
        stage.register_metadata(dict(name="pkg1", version="1.0"))
        content = zip_dict({"index.html": "<html/>",
            "_static": {}, "_templ": {"x.css": ""}})
        stage.store_doczip("pkg1", "1.0", content)
        doczip = stage.get_doczip("pkg1", "1.0")
        assert doczip
        with Archive(BytesIO(doczip)) as archive:
            archive.extract(tmpdir)
        assert tmpdir.join("index.html").read() == "<html/>"
        assert tmpdir.join("_static").check(dir=1)
        assert tmpdir.join("_templ", "x.css").check(file=1)

    def test_storedoczipfile(self, stage, bases):
        from devpi_common.archive import Archive
        stage.register_metadata(dict(name="pkg1", version="1.0"))
        content = zip_dict({"index.html": "<html/>",
            "_static": {}, "_templ": {"x.css": ""}})
        stage.store_doczip("pkg1", "1.0", content)
        archive = Archive(BytesIO(stage.get_doczip("pkg1", "1.0")))
        assert 'index.html' in archive.namelist()

        content = zip_dict({"nothing": "hello"})
        stage.store_doczip("pkg1", "1.0", content)
        archive = Archive(BytesIO(stage.get_doczip("pkg1", "1.0")))
        namelist = archive.namelist()
        assert 'nothing' in namelist
        assert 'index.html' not in namelist
        assert '_static' not in namelist
        assert '_templ' not in namelist

    def test_store_and_get_volatile(self, stage):
        stage.modify(volatile=False)
        content = b"123"
        content2 = b"1234"
        entry = register_and_store(stage, "some-1.0.zip", content)
        assert len(stage.getreleaselinks("some")) == 1

        # rewrite  fails
        entry = stage.store_releasefile("some", "1.0",
                                        "some-1.0.zip", content2)
        assert entry == 409

        # rewrite succeeds with volatile
        stage.modify(volatile=True)
        entry = stage.store_releasefile("some", "1.0",
                                        "some-1.0.zip", content2)
        entries = stage.getreleaselinks("some")
        assert len(entries) == 1
        assert entries[0].FILE.get() == content2

    def test_releasedata(self, stage):
        assert stage.metadata_keys
        assert not stage.get_metadata("hello", "1.0")
        stage.register_metadata(dict(name="hello", version="1.0", author="xy"))
        d = stage.get_metadata("hello", "1.0")
        assert d["author"] == "xy"
        #stage.ixconfig["volatile"] = False
        #with pytest.raises(stage.MetadataExists):
        #    stage.register_metadata(dict(name="hello", version="1.0"))
        #

    def test_filename_version_mangling_issue68(self, stage):
        assert not stage.get_metadata("hello", "1.0")
        metadata = dict(name="hello", version="1.0-test")
        stage.register_metadata(metadata)
        stage.store_releasefile("hello", "1.0-test",
                            "hello-1.0_test.whl", b"")
        ver = stage.get_metadata_latest_perstage("hello")
        assert ver["version"] == "1.0-test"
        #stage.ixconfig["volatile"] = False
        #with pytest.raises(stage.MetadataExists):
        #    stage.register_metadata(dict(name="hello", version="1.0"))
        #

    def test_get_metadata_latest(self, stage):
        stage.register_metadata(dict(name="hello", version="1.0"))
        stage.register_metadata(dict(name="hello", version="1.1"))
        stage.register_metadata(dict(name="hello", version="0.9"))
        metadata = stage.get_metadata_latest_perstage("hello")
        assert metadata["version"] == "1.1"

    def test_get_metadata_latest_inheritance(self, user, model, stage):
        stage_base_name = stage.index + "base"
        user.create_stage(index=stage_base_name, bases=(stage.name,))
        stage_sub = model.getstage(stage.user.name, stage_base_name)
        stage_sub.register_metadata(dict(name="hello", version="1.0"))
        stage.register_metadata(dict(name="hello", version="1.1"))
        metadata = stage_sub.get_metadata_latest_perstage("hello")
        assert metadata["version"] == "1.0"
        metadata = stage.get_metadata_latest_perstage("hello")
        assert metadata["version"] == "1.1"

    def test_releasedata_validation(self, stage):
        with pytest.raises(ValueError):
             stage.register_metadata(dict(name="hello_", version="1.0"))

    def test_register_metadata_normalized_name_clash(self, stage):
        stage.register_metadata(dict(name="hello-World", version="1.0"))
        with pytest.raises(stage.RegisterNameConflict):
            stage.register_metadata(dict(name="Hello-world", version="1.0"))
            stage.register_metadata(dict(name="Hello_world", version="1.0"))

    def test_get_existing_project(self, stage):
        stage.register_metadata(dict(name="Hello", version="1.0"))
        stage.register_metadata(dict(name="this", version="1.0"))
        project = stage.get_project_info("hello")
        assert project.name == "Hello"

    def test_releasedata_description(self, stage):
        source = py.builtin._totext(dedent("""\
            test the *world*
        """))
        assert stage.metadata_keys
        assert not stage.get_description("hello", "1.0")
        stage.register_metadata(dict(name="hello", version="1.0",
                description=source))
        html = stage.get_description("hello", "1.0")
        assert py.builtin._istext(html)
        assert "test" in html and "world" in html

    def test_releasedata_description_versions(self, stage):
        stage.register_metadata(dict(name="hello", version="1.0",
            description=py.builtin._totext("hello")))
        stage.register_metadata(dict(name="hello", version="1.1",
            description=py.builtin._totext("hello")))
        ver = stage.get_description_versions("hello")
        assert set(ver) == set(["1.0", "1.1"])


class TestUsers:

    def test_secret(self, xom, model):
        xom.keyfs.basedir.ensure(".something")
        assert model.get_user(".something") is None

    def test_create_and_validate(self, model):
        user = model.get_user("user")
        assert not user
        user = model.create_user("user", "password", email="some@email.com")
        assert user
        userconfig = user.get()
        assert userconfig["email"] == "some@email.com"
        assert not set(userconfig).intersection(["pwsalt", "pwhash"])
        assert user.validate("password")
        assert not user.validate("password2")

    def test_create_and_delete(self, model):
        user = model.create_user("user", password="password")
        user.delete()
        assert not user.validate("password")

    def test_create_and_list(self, model):
        baselist = model.get_usernames()
        model.create_user("user1", password="password")
        model.create_user("user2", password="password")
        model.create_user("user3", password="password")
        newusers = model.get_usernames().difference(baselist)
        assert newusers == set("user1 user2 user3".split())
        model.get_user("user3").delete()
        newusers = model.get_usernames().difference(baselist)
        assert newusers == set("user1 user2".split())

    def test_server_passwd(self, model, monkeypatch):
        monkeypatch.setattr(py.std.getpass, "getpass", lambda x: "123")
        run_passwd(model, "root")
        assert model.get_user("root").validate("123")

    def test_server_email(self, model):
        email_address = "root_" + str(id) + "@mydomain"
        user = model.get_user("root")
        user.modify(email=email_address)
        assert user.get()["email"] == email_address

def test_user_set_without_indexes(model):
    user = model.create_user("user", "password", email="some@email.com")
    user.create_stage("hello")
    user._set({"password": "pass2"})
    assert model.getstage("user/hello")

@pytest.mark.notransaction
def test_setdefault_indexes(model):
    from devpi_server.main import set_default_indexes
    set_default_indexes(model)
    with model.keyfs.transaction(write=False):
        assert model.getstage("root/pypi").ixconfig["type"] == "mirror"
