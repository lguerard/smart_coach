# Politique de confidentialité

smart_coach est un outil auto-hébergé, à usage strictement personnel :
chaque déploiement tourne sur le serveur de son propre utilisateur, avec
sa propre base de données, et n'est exploité par personne d'autre que lui
(ou les comptes qu'il crée lui-même pour son foyer).

## Données concernées

L'application lit, via l'accès Google Calendar demandé au moment de la
connexion, les évènements nécessaires pour :

- vérifier les créneaux déjà occupés avant de programmer une séance
  d'entraînement ;
- créer/mettre à jour les évènements de séance sur le calendrier choisi
  par l'utilisateur.

Elle combine ces informations avec des données de santé et d'activité
(Garmin Connect, export Health Connect) déjà présentes sur l'appareil
et les comptes de l'utilisateur, dans le seul but de générer son propre
message de coaching quotidien.

## Stockage et partage

- Toutes les données restent dans la base de données locale du serveur
  auto-hébergé par l'utilisateur. Rien n'est envoyé à un service tiers
  autre que ceux explicitement configurés par l'utilisateur lui-même
  (Google Calendar, l'API Garmin Connect, et le fournisseur de modèle
  de langage choisi pour générer le message).
- Aucune donnée n'est vendue, partagée avec des annonceurs, ou utilisée
  à des fins autres que le fonctionnement de cette instance personnelle.
- Le jeton d'accès Google Calendar est stocké localement sur le serveur
  de l'utilisateur et n'est jamais transmis ailleurs.

## Suppression des données

L'utilisateur contrôle entièrement son propre déploiement : supprimer le
conteneur et son volume de données efface l'intégralité des informations
stockées. Révoquer l'accès depuis
[myaccount.google.com/permissions](https://myaccount.google.com/permissions)
retire immédiatement l'accès au calendrier.

## Contact

Ce projet est développé et maintenu par son auteur sur
[github.com/lguerard/smart_coach](https://github.com/lguerard/smart_coach) ;
toute question peut y être posée via une issue GitHub.
