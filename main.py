import configparser
import os
import logging
import copy
import json
import datetime
import hashlib

from urllib.parse import urlencode
from functools    import wraps

from flask import Flask, render_template, request, redirect, session, url_for, escape, Response

from flask.ext.login import LoginManager, login_user, logout_user, current_user, login_required
from flask.ext.cache import Cache

from flask_wtf import Form, RecaptchaField
from wtforms import TextField


import evelink.api

from spearmint_libs.utils import Utils
from spearmint_libs.pi    import Pi
from spearmint_libs.auth  import Auth
from spearmint_libs.user  import db, User, Character
from spearmint_libs.emailtools import EmailTools


with open("config.json") as cfg:
    config = json.loads(cfg.read())

assert(config)


eve = evelink.eve.EVE()
emailtools = EmailTools(config)

app = Flask(__name__)
app.config.update(config)

app.config['DEBUG'] = False


app.config['SECRET_KEY']              = os.urandom(1488)
app.config['SQLALCHEMY_DATABASE_URI'] = app.config['database']['uri']


db.init_app(app)

login_manager  = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)

logging.basicConfig(filename=app.config['general']['log_path'], level=logging.DEBUG)

# Setup the corp api object and get the corp ID
corp_api = evelink.api.API(api_key=(app.config['corp_api']['key'], app.config['corp_api']['code']))
corp     = evelink.corp.Corp(corp_api)
app.config['corp_id']  = corp.corporation_sheet()[0]['id']


utils = Utils(app.config)
pi    = Pi(app.config, utils)
cache = Cache(app,config={'CACHE_DIR':app.config['general']['cache_dir'], 'CACHE_TYPE': app.config['general']['cache_type']})

def format_time(timestamp):
    if timestamp:
        return datetime.datetime.utcfromtimestamp(timestamp).isoformat()
    else:
        return 'N/A'

def format_currency(amount):
    return '{:,.2f}'.format(amount)

@cache.memoize()
def character_name_from_id(id_):
    return eve.character_name_from_id(id_)[0]

def corp_name_from_corp_id(id_):
    corp_name = eve.affiliations_for_characters(id_)
    return corp_name[0][id_]['name']


app.jinja_env.filters['format_time'] = format_time
app.jinja_env.filters['format_currency'] = format_currency
app.jinja_env.filters['character_name_from_id'] = character_name_from_id
app.jinja_env.filters['corp_name_from_corp_id'] = corp_name_from_corp_id

class RegisterForm(Form):
    keyid = TextField('KeyID')
    code  = TextField('Code')

    recaptcha = RecaptchaField()


class UserChangePassword(Form):
    password    = TextField('Password')
    verify_pass = TextField('Verify Password')



def check_auth(email, password):
    query = User.query.filter_by(email=email).first()
    if query:
        a = Auth(email, password)
        if a.check_password(password):
            logging.info('[check_auth] correct password for: %s' % (query.email))
            return True
        else:
            logging.info('[check_auth] incorrect password for: %s' % (query.email))
    else:
        logging.info("[check_auth] couldn't find %s for authentication" % (email))
    return False

def generate_code():
    return hashlib.sha1(os.urandom(1488)).hexdigest()


@login_manager.user_loader 
def load_user(id):
    query = User.query.filter_by(email=id).first()
    return query or None


@app.route('/login', methods=['POST','GET'])
def login():
    if request.method == 'POST':

        if check_auth(request.form.get('email'), request.form.get('password')):
            to_login = load_user(request.form.get('email'))
            
            if login_user(to_login):
                logging.info('[login] logged in: %s' % (current_user.email))
                

                # Fix this, it needs to actually redirect. 
                next_page = request.form.get('next')

                if next_page:
                    return redirect(next_page)

                else:
                    return redirect('/')
                
        else:
            return render_template('info.html', info='Incorrect email/password combination')

    
    return render_template('login.html')


@app.route('/logout', methods=['POST', 'GET'])
def logout():
    if current_user.is_authenticated():
        logout_user()
        return render_template('info.html', info='Successfully logged out')

    return render_template('info.html', info='You are not logged in')


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/register', methods=['POST', 'GET'])
def register():

    if request.method == 'POST':

        logging.info('[register] request.form: %s' % (request.form))

        api = evelink.api.API(api_key=(request.form.get('keyid'), request.form.get('code')))
        account = evelink.account.Account(api)

        try:
            characters = account.characters()

        except Exception as ex: 
            logging.warning('[register] exception: %s' % (ex))
            return render_template('info.html', info="It dosen't seem like you correctly entered your API")

        if characters.result:
            # Store in seperate dictionary so I can edit it. 
            char_copy = copy.deepcopy(characters.result)

            # Remove characters that are not in the corp.
            for c in char_copy:
                if char_copy[c]['corp']['id'] != app.config['corp_id']:
                    # Remove from the original dictionary.
                    logging.info('[register] removing character: %s' % (characters.result[c]))
                    del characters.result[c]
               
            # Check to see if we still have any characters left. 
            if characters.result:
                logging.info('[register] characters.result: %s' % characters.result)

                session['characters'] = characters.result
                session['api_code']   = request.form.get('register_code')
                session['api_key_id']  = request.form.get('register_keyid')

                return redirect(url_for('confirm_register'))

            else:
                return render_template('info.html', info='None of your characters are in the corporation')
    
    return render_template('register.html', form=RegisterForm())


@app.route('/confirm_register', methods=['POST', 'GET'])
def confirm_register():
    if request.method == 'POST': 

      
        auth  = Auth(request.form.get('register_email'), request.form.get('register_password'))
        email = request.form.get('register_email')
       
        
        logging.info('[confirm_register] password hash: %s' % (auth.pw_hash))

        # Make sure user isn't already in the db. 
        query = User.query.filter_by(email=request.form.get('register_email')).first()

        if query:
            if request.form.get('register_email') == query.email:
                return render_template('info.html', info='You have already registered')


        
        # Add the user to the db and generate the password hash.
        activation_code = generate_code()
        
        user = User(email=request.form.get('register_email'), 
                    password=auth.pw_hash,
                    api_key_id=session['api_key_id'],
                    api_code=session['api_code'],
                    active=False,
                    activation_code=activation_code)
     
        db.session.add(user)
        db.session.commit()

        
        for c in session['characters']:
            db.session.add(Character(character_id=c, user=user))
            db.session.commit() 


        activation_link = 'http://%s/activate_account?activation_code=%s&email=%s' % (config['general']['hostname'], activation_code, email)

        emailtools.send_email(to=email, 
                              subject='Activate your account', 
                              body=activation_link)

        return render_template('submitted_register.html')

    return render_template('confirm_register.html', characters=session['characters'])


@app.route('/reset_password', methods=['POST','GET'])
def email_reset_password():
    if request.method == 'POST':
        email = request.form.get('email')

        if not email:
            return render_template('info.html', info='Missing email')

        query = User.query.filter_by(email=email).first()

        if not query:
            return render_template('info.html', info='User not found')

        recovery_code = generate_code() 
        query.recovery_code = recovery_code
        query.recovery_timestamp = datetime.datetime.now()

        db.session.commit()

        recovery_link = 'http://%s/password_recovery?recovery_code=%s&email=%s' % (config['general']['hostname'], recovery_code, email)

        emailtools.send_email(to=email,
                              subject='Password recovery',
                              body=recovery_link)

        return render_template('info.html', info='Recovery email has been sent')

    return render_template('reset_password.html')


@app.route('/password_recovery', methods=['POST', 'GET'])
def reset_password():
    if request.method == 'GET':
        email         = request.args.get('email')
        recovery_code = request.args.get('recovery_code')
   
        if not email or not recovery_code:
            return render_template('info.html', info='Invalid recovery code or email')

        query = User.query.filter_by(email=email).first()

        # Gotta be pretty 1337 to get this far
        if not query:
            return render_template('info.html', info='Account not found')

        time_elapsed = (datetime.datetime.now() - query.recovery_timestamp) / 3600.0 
        if recovery_code == query.recovery_code:
            login_user(query)
            return redirect(url_for('user/index'))

    return render_template('password_recovery.html')



@app.route('/user/index', methods=['POST', 'GET'])
@login_required
def user_settings():
    return render_template('user/index.html')

@app.route('/user/settings/password', methods=['POST', 'GET'])
@login_required
def user_change_password():

    if request.method == 'POST':
        password        = request.form.get('password')
        verify_password = request.form.get('verify_password')

        if len(password) and len(verify_password):
            if password == verify_password:
                auth = Auth(current_user.email, password)
                current_user.password = auth.pw_hash
                db.session.commit()

                return render_template('info.html', info='Password successfully updated.')

            else:
                return render_template('info.html', info='Passwords do not match')
        
        return render_template('info.html', info='No password entered')

    return render_template('user/settings/password.html', form=UserChangePassword())


@app.route('/activate_account', methods=['GET'])
def activate_account():
    email = request.args.get('email')
    activation_code  = request.args.get('activation_code')

    query = User.query.filter_by(email=email).first()  

    if not query or not email or not activation_code:
        return render_template('info.html', info='There is an issue trying to activate your account, please contact the admin.')

   
    # See if the activation code matches, and if it does "activate" the account, and set the previously used
    # code to 'NULL'
    if activation_code == query.activation_code and query.activation_code != 'NULL':
        query.activation_code = 'NULL'
        query.active = True
        query.activation_timestamp = datetime.datetime.now()
        db.session.commit()

        #time_elapsed = (datetime.datetime.now() - query.activation_timestamp).total_seconds() / 3600.0
       
        # If time has exceeded 1 week
        #if time_elapsed > 168:
        #    return render_template('info.html', info='Activation code is expired')

        #else:
        #    return render_template('info.html', info='Good code')

        return render_template('info.html', info='Your account has been activated')
    
    return render_template('info.html', info='Invalid activation code or email')

@app.route('/pi_statistics/<int:tier>', methods=['GET', 'POST'])
def pi_statistics(tier):

    results = {}
    systems = ['jita', 'amarr']

    for system_name in systems:
        system = utils.search_system(system_name)
        data = pi.get_prices(tier, system['solarSystemID'])

        if data:
            results[system['solarSystemName'].lower()] = {"data":data, "cached_time":data[0].date}

    return render_template('pi_statistics.html', results=results)


@app.route('/corp/index', methods=['GET'])
@login_required
def corp_index():
    return render_template('corp/index.html')


@app.route('/corp/standings', methods=['GET'])
@login_required
def corp_standings():
    return render_template('corp/standings.html', standings=corp.npc_standings())


@app.route('/corp/wallet_transactions',  methods=['GET'])
@login_required
def corp_transactions():
    return render_template('corp/wallet_transactions.html', wallet_transactions=corp.wallet_transactions())

@app.route('/corp/contracts', methods=['GET'])
@login_required
def corp_contracts():

    contracts = corp.contracts()[0]

    return render_template('corp/contracts.html', contracts=contracts)


if __name__ == '__main__':
    app.run()
