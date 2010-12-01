#!/usr/bin/env python
import os,socket, sys
import json

from fabric.state import _AttributeDict, env
from fabric.operations import run, sudo
from fabric.context_managers import cd, settings
from fabric.contrib.files import append, contains, exists
from fabric.decorators import runs_once

from woven.deployment import deploy_files, mkdirs, run_once_per_host_version, upload_template
from woven.environment import deployment_root, server_state, _root_domain
from woven.ubuntu import add_user

def _activate_sites(path, filenames):
    enabled_sites = _ls_sites(path)            
    for site in enabled_sites:
        if env.verbosity:
            print env.host,'Disabling', site
        if site not in filenames:
            sudo("rm %s/%s"% (path,site))
        
        sudo("chmod 644 %s" % site)
        if not exists('/etc/apache2/sites-enabled'+ filename):
            sudo("ln -s %s%s %s%s"% (self.deploy_root,filename,self.enabled_path,filename))

def _deploy_webconf(remote_dir,template):
    
    if not 'http:' in env.MEDIA_URL: media_url = env.MEDIA_URL
    else: media_url = ''
    if not 'http:' in env.STATIC_URL: static_url = env.STATIC_URL
    else: static_url = ''
    if not static_url: static_url = env.ADMIN_MEDIA_PREFIX
    log_dir = '/'.join([deployment_root(),'log'])
    deployed = []
    users_added = []
    
    domains = domain_sites()
    for d in domains:
        u_domain = d.name.replace('.','_')
        wsgi_filename = d.settings.replace('.py','.wsgi')
        site_user = ''.join(['site_',str(d.site_id)])
        filename = ''.join([remote_dir,'/',u_domain,'-',env.project_version,'.conf'])
        context = {"project_name": env.project_name,
                   "deployment_root":deployment_root(),
                    "u_domain":u_domain,
                    "domain":d.name,
                    "root_domain":env.root_domain,
                    "user":env.user,
                    "site_user":site_user,
                    "host_ip":socket.gethostbyname(env.host),
                    "wsgi_filename":wsgi_filename,
                    "MEDIA_URL":media_url,
                    "STATIC_URL":static_url,
                    }

        upload_template('/'.join(['woven',template]),
                        filename,
                        context,
                        use_sudo=True)
        if env.verbosity:
            print " * uploaded", filename
            
        #add site users if necessary
        site_users = _site_users()
        if site_user not in users_added and site_user not in site_users:
            add_user(username=site_user,group='www-data',site_user=True)
            users_added.append(site_user)
            if env.verbosity:
                print " * useradded",site_user

    return deployed

def _site_users():
    """
    Get a list of site_n users
    """
    userlist = sudo("cat /etc/passwd | awk '/site/'").split('\n')
    siteuserlist = [user.split(':')[0] for user in userlist if 'site_' in user]
    return siteuserlist

def _ls_sites(path):
    """
    List only sites in the domain_sites() to ensure we co-exist with other projects
    """
    with cd(path):
        sites = run('ls').split('\n')
        doms =  [d.name for d in domain_sites()]
        dom_sites = []
        for s in sites:
            ds = s.split('-')[0]
            ds = ds.replace('_','.')
            if ds in doms and s not in dom_sites:
                dom_sites.append(s)
    return dom_sites



def _sitesettings_files():
    """
    Get a list of sitesettings files
    
    settings.py can be prefixed with a subdomain and underscore so with example.com site:
    sitesettings/settings.py would be the example.com settings file and
    sitesettings/admin_settings.py would be the admin.example.com settings file
    """
    settings_files = []
    sitesettings_path = os.path.join(env.project_package_name,'sitesettings')
    if os.path.exists(sitesettings_path):
        sitesettings = os.listdir(sitesettings_path)
        for file in sitesettings:
            if file == 'settings.py':
                settings_files.append(file)
            elif len(file)>12 and file[-12:]=='_settings.py': #prefixed settings
                settings_files.append(file)
    return settings_files

def _get_django_sites():
    """
    Get a list of sites as dictionaries {site_id:'domain.name'}

    """
    deployed = server_state('deploy_project')
    if not env.sites and 'django.contrib.sites' in env.INSTALLED_APPS and deployed:
        with cd('/'.join([deployment_root(),'env',env.project_fullname,'project',env.project_package_name,'sitesettings'])):
            venv = '/'.join([deployment_root(),'env',env.project_fullname,'bin','activate'])
            #since this is the first time we run ./manage.py on the server it can be
            #a point of failure for installations
            with settings(warn_only=True):
                output = run(' '.join(['source',venv,'&&',"./manage.py dumpdata sites"]))

                if output.failed:
                    print "ERROR: There was an error running ./manage.py on the node"
                    print "See the troubleshooting docs for hints on how to diagnose deployment issues"
                    if hasattr(output, 'stderr'):
                        print output.stderr
                    sys.exit(1)
            output = output.split('\n')[-1] #ignore any lines prior to the data being dumped
            sites = json.loads(output)
            env.sites = {}
            for s in sites:
                env.sites[s['pk']] = s['fields']['domain']
    return env.sites

def domain_sites():
    """
    Get a list of domains
    
    Each domain is an attribute dict with name, site_id and settings
    """

    if not hasattr(env,'domains'):
        sites = _get_django_sites()
        site_ids = sites.keys()
        site_ids.sort()
        domains = []
        
        for id in site_ids:

            for file in _sitesettings_files():
                domain = _AttributeDict({})

                if file == 'settings.py':
                    domain.name = sites[id]
                else: #prefix indicates subdomain
                    subdomain = file[:-12].replace('_','.')
                    domain.name = ''.join([subdomain,sites[id]])

                domain.settings = file
                domain.site_id = id
                domains.append(domain)
                
        env.domains = domains
        if env.domains: env.root_domain = env.domains[0].name
        else:
            domain.name = _root_domain(); domain.site_id = 1; domain.settings='settings.py'
            env.domains = [domain]
            
    return env.domains

@run_once_per_host_version
def deploy_webconf():
    """ Deploy apache & nginx site configurations to the host """
    deployed = []
    log_dir = '/'.join([deployment_root(),'log'])
    #TODO - incorrect - check for actual package to confirm installation
    if exists('/etc/apache2/sites-enabled/') and exists('/etc/nginx/sites-enabled'):
        if env.verbosity:
            print env.host,"DEPLOYING webconf:"
        if not exists(log_dir):
            run('ln -s /var/log log')
        #deploys confs for each domain based on sites app
        deployed += _deploy_webconf('/etc/apache2/sites-available','django-apache-template.txt')
        deployed += _deploy_webconf('/etc/nginx/sites-available','nginx-template.txt')
        upload_template('woven/maintenance.html','/var/www/nginx-default/maintenance.html',use_sudo=True)
        sudo('chmod ugo+r /var/www/nginx-default/maintenance.html')
    else:
        print env.host,"""WARNING: Apache or Nginx not installed"""
        
    return deployed

@run_once_per_host_version
def deploy_wsgi():
    """
    deploy python wsgi file(s)
    """
    remote_dir = '/'.join([deployment_root(),'env',env.project_fullname,'wsgi'])
    deployed = []
    
    #ensure project apps path is also added to environment variables as well as wsgi
    if env.PROJECT_APPS_PATH:
        pap = '/'.join([deployment_root(),'env',
                        env.project_name,'project',env.project_package_name,env.PROJECT_APPS_PATH])
        pap = ''.join(['export PYTHONPATH=$PYTHONPATH:',pap])
        postactivate = '/'.join([deployment_root(),'env','postactivate'])
        if not exists(postactivate):
            append('#!/bin/bash', postactivate)
            run('chmod +x %s'% postactivate)
        if not contains('PYTHONPATH',postactivate):
            append(pap,postactivate)
        
    if env.verbosity:
        print env.host,"DEPLOYING wsgi", remote_dir

    for file in _sitesettings_files(): 
        deployed += mkdirs(remote_dir)
        with cd(remote_dir):
            filename = file.replace('.py','.wsgi')
            context = {"deployment_root":deployment_root(),
                       "user": env.user,
                       "project_name": env.project_name,
                       "project_package_name": env.project_package_name,
                       "project_apps_path":env.PROJECT_APPS_PATH,
                       "settings": filename.replace('.wsgi',''),
                       }
            upload_template('/'.join(['woven','django-wsgi-template.txt']),
                                filename,
                                context,
                            )
            if env.verbosity:
                print " * uploaded", filename
            #finally set the ownership/permissions
            #We'll use the group to allow www-data execute
            sudo("chown %s:www-data %s"% (env.user,filename))
            run("chmod ug+xr %s"% filename)
    return deployed

def has_webservers():
    """
    simple test for webserver packages
    """
    p = set(env.packages)
    w = set(['apache','nginx'])
    installed = p & w
    if installed: return True
    else: return False
    
def reload_webservers():
    """
    Reload apache2 and nginx
    """
    if env.verbosity:
        print env.host, "RELOADING apache2"
    with settings(warn_only=True):
        a = sudo("/etc/init.d/apache2 reload")
        if env.verbosity:
            print '',a        
    if env.verbosity:

        #Reload used to fail on Ubuntu but at least in 10.04 it works
        print env.host,"RELOADING nginx"
    with settings(warn_only=True):
        s = run("/etc/init.d/nginx status")
        if 'running' in s:
            n = sudo("/etc/init.d/nginx reload")
        else:
            n = sudo("/etc/init.d/nginx start")
    if env.verbosity:
        print ' *',n
    return True    

def stop_webservers():
    """
    Stop apache2
    """
    #TODO - distinguish between a warning and a error on apache
    with settings(warn_only=True):
        if env.verbosity:
            print env.host,"STOPPING apache2"
        a = sudo("/etc/init.d/apache2 stop")
        if env.verbosity:
            print '',a
        
    return True

def start_webservers():
    """
    Start apache2 and start/reload nginx
    """
    with settings(warn_only=True):
        if env.verbosity:
            print env.host,"STARTING apache2"
        a = sudo("/etc/init.d/apache2 start")
        if env.verbosity:
            print '',a
        
    if a.failed:
        print "ERROR: /etc/init.d/apache2 start failed"
        print env.host, a
        sys.exit(1)
    if env.verbosity:
        #Reload used to fail on Ubuntu but at least in 10.04 it works
        print env.host,"RELOADING nginx"
    with settings(warn_only=True):
        s = run("/etc/init.d/nginx status")
        if 'running' in s:
            n = sudo("/etc/init.d/nginx reload")
        else:
            n = sudo("/etc/init.d/nginx start")
    if env.verbosity:
        print ' *',n
    return True

    