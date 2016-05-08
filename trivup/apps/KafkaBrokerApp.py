from trivup import trivup
from trivup.apps.KerberosKdcApp import KerberosKdcApp

import os
import time


class KafkaBrokerApp (trivup.App):
    """ Kafka broker app
        Depends on ZookeeperApp """
    def __init__(self, cluster, conf=None, on=None, kafka_path=None):
        """
        @param cluster     Current cluster
        @param conf        Configuration dict, see below.
        @param kafka_path  Path to Kafka build tree (for trunk usage)
        @param on          Node name to run on

        Supported conf keys:
           * version - Kafka version to use, will build 'trunk' from kafka_path,
                       otherwise the version is taken to be a formal release which
                       will be downloaded and deployed.
           * listeners - CSV list of listener types: PLAINTEXT,SSL,SASL,SSL_SASL
           * sasl_mechanisms - CSV list of SASL mechanisms to enable: GSSAPI,PLAIN
                               SASL listeners will be added automatically.
                               KerberosKdcApp is required for GSSAPI.
           * sasl_users - CSV list of SASL PLAIN of user=pass for authenticating clients
           * num_partitions - Topic auto-create partition count (3)
           * replication_Factor - Topic auto-create replication factor (1)
        """
        super(KafkaBrokerApp, self).__init__(cluster, conf=conf, on=on)

        self.zk = cluster.find_app('ZookeeperApp')
        if self.zk is None:
            raise Exception('ZookeeperApp required')

        # Kafka repo uses SVN nomenclature
        if self.conf['version'] == 'master':
            self.conf['version'] = 'trunk'

        # Arbitrary (non-template) configuration statements
        conf_blob = list()
        jaas_blob = list()

        #
        # Configure listeners, SSL and SASL
        #
        listeners = self.conf.get('listeners', 'PLAINTEXT').split(',')

        # SASL support
        sasl_mechs = [x for x in self.conf.get('sasl_mechanisms', '').replace(' ', '').split(',') if len(x) > 0]
        if len(sasl_mechs) > 0:
            listeners.append('SASL_PLAINTEXT')

        # Create listeners
        ports = [(x, trivup.TcpPortAllocator(self.cluster).next()) for x in sorted(set(listeners))]
        self.conf['port'] = ports[0][1] # "Default" port
        self.conf['address'] = '%(nodename)s:%(port)d' % self.conf
        self.conf['listeners'] = ','.join(['%s://%s:%d' % (x[0], self.node.name, x[1]) for x in ports])
        self.conf['advertised.listeners'] = self.conf['listeners']
        self.dbg('Listeners: %s' % self.conf['listeners'])

        self.conf['kafka_path'] = kafka_path

        if len(sasl_mechs) > 0:
            self.dbg('SASL mechanisms: %s' % sasl_mechs)
            jaas_blob.append('KafkaServer {')

            conf_blob.append('sasl.enabled.mechanisms=%s' % ','.join(sasl_mechs))
            if 'PLAIN' in sasl_mechs:
                sasl_users = self.conf.get('sasl_users', '')
                if len(sasl_users) == 0:
                    self.log('WARNING: No sasl_users configured for PLAIN, expected CSV of user=pass,..')
                else:
                    jaas_blob.append('org.apache.kafka.common.security.plain.PlainLoginModule required debug=true')
                    for up in sasl_users.split(','):
                        u,p = up.split('=')
                        jaas_blob.append('user_%s="%s"' % (u, p))
                    jaas_blob[-1] += ';'

            if 'GSSAPI' in sasl_mechs:
                conf_blob.append('sasl.kerberos.service.name=%s' % 'kafka')
                kdc = self.cluster.find_app(KerberosKdcApp)
                self.env_add('KRB5_CONFIG', kdc.conf['krb5_conf'])
                self.env_add('KAFKA_OPTS', '-Djava.security.krb5.conf=%s' % kdc.conf['krb5_conf'])
                self.env_add('KAFKA_OPTS', '-Dsun.security.krb5.debug=true')
                self.kerberos_principal,self.kerberos_keytab = kdc.add_principal('kafka', self.node.name)
                jaas_blob.append('com.sun.security.auth.module.Krb5LoginModule required')
                jaas_blob.append('useKeyTab=true storeKey=true doNotPrompt=true')
                jaas_blob.append('keyTab="%s"' % self.kerberos_keytab)
                jaas_blob.append('debug=true')
                jaas_blob.append('principal="%s";' % self.kerberos_principal)

            jaas_blob.append('};\n')
            self.conf['jaas_file'] = self.create_file('jaas_broker.conf', data='\n'.join(jaas_blob))
            self.env_add('KAFKA_OPTS', '-Djava.security.auth.login.config=%s' % self.conf['jaas_file'])
            self.env_add('KAFKA_OPTS', '-Djava.security.debug=all')


        # Kafka Configuration properties
        self.conf['log_dirs'] = self.create_dir('logs')
        if 'num_partitions' not in self.conf:
            self.conf['num_partitions'] = 3
        self.conf['zk_connect'] = self.zk.get('address', None)
        if 'replication_factor' not in self.conf:
            self.conf['replication_factor'] = 1

        # Generate config file
        self.conf['conf_file'] = self.create_file_from_template('server.properties',
                                                                self.conf,
                                                                append_data='\n'.join(conf_blob))

        # Generate LOG4J file file
        self.conf['log4j_file'] = self.create_file_from_template('log4j.properties', self.conf, subst=False)
        self.env_add('KAFKA_LOG4J_OPTS', '-Dlog4j.configuration=file:%s' % self.conf['log4j_file'])

        # Runs in foreground, stopped by Ctrl-C
        # This is the default for no-deploy use: will be overwritten by deploy() if enabled.
        if kafka_path:
            start_sh = os.path.join(kafka_path, 'bin', 'kafka-server-start.sh')
        else:
            start_sh = 'kafka-server-start.sh'

        self.conf['start_cmd'] = '%s %s' % (start_sh, self.conf['conf_file'])
        self.conf['stop_cmd'] = None # Ctrl-C

    def operational (self):
        self.dbg('Checking if operational')
        return os.system('(echo anything | nc %s) 2>/dev/null' %
                         ' '.join(self.get('address').split(':'))) == 0


    def deploy (self):
        destdir = os.path.join(self.cluster.mkpath(self.__class__.__name__), 'kafka', self.get('version'))
        self.dbg('Deploy %s version %s on %s to %s' %
                 (self.name, self.get('version'), self.node.name, destdir))
        deploy_exec = self.resource_path('deploy.sh')
        if not os.path.exists(deploy_exec):
            raise NotImplementedError('Kafka deploy.sh script missing in %s' %
                                      deploy_exec)
        t_start = time.time()
        cmd = '%s %s "%s" "%s"' % \
              (deploy_exec, self.get('version'), self.get('kafka_path'), destdir)
        r = os.system(cmd)
        if r != 0:
            raise Exception('Deploy "%s" returned exit code %d' % (cmd, r))
        self.dbg('Deployed version %s in %ds' %
                 (self.get('version'), time.time() - t_start))

        # Override start command with updated path.
        self.conf['start_cmd'] = '%s/bin/kafka-server-start.sh %s' % (destdir, self.conf['conf_file'])
        self.dbg('Updated start_cmd to %s' % self.conf['start_cmd'])